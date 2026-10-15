"""Carte locale des bus et trams de Montpellier à partir des flux open data."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import difflib
from html.parser import HTMLParser
import io
import json
import math
import os
import re
import sqlite3
import tempfile
import threading
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from flask import Flask, jsonify, redirect, render_template, request
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2


VEHICLE_POSITIONS_URL = os.getenv(
    "TAM_VEHICLE_POSITIONS_URL",
    "https://gtfsproxy.e-tam.fr/COMMON/VehiclePosition.pb",
)
TRAM_LINES_FILE = Path(os.getenv(
    "TAM_TRAM_LINES_FILE",
    str(Path(__file__).resolve().parent / "data" / "MMM_MMM_LigneTram.json"),
)).resolve()
BUS_LINES_FILE = Path(os.getenv(
    "TAM_BUS_LINES_FILE",
    str(Path(__file__).resolve().parent / "data" / "MMM_MMM_BusLigne.json"),
)).resolve()
BUSTRAM_LINES_FILE = Path(os.getenv(
    "TAM_BUSTRAM_LINES_FILE",
    str(Path(__file__).resolve().parent / "data" / "MMM_MMM_Bustram.json"),
)).resolve()
GTFS_URL = os.getenv(
    "TAM_GTFS_URL",
    "https://gtfsproxy.e-tam.fr/COMMON/GTFS.zip",
)
TRIP_UPDATES_URL = os.getenv(
    "TAM_TRIP_UPDATES_URL",
    "https://gtfsproxy.e-tam.fr/COMMON/TripUpdate.pb",
)
TRAM_STOPS_URL = os.getenv(
    "TAM_TRAM_STOPS_URL",
    "https://data.montpellier3m.fr/sites/default/files/ressources/MMM_MMM_ArretsTram.json",
)
BUS_STOPS_URL = os.getenv(
    "TAM_BUS_STOPS_URL",
    "https://data.montpellier3m.fr/sites/default/files/ressources/MMM_MMM_ArretsBus.json",
)
# The page still refreshes every 15 seconds, but share an upstream request
# across several browser refreshes so the TAM proxy is not polled as often.
VEHICLE_CACHE_SECONDS = 30
VEHICLE_MAX_AGE_SECONDS = 400
VEHICLE_429_MIN_BACKOFF_SECONDS = 30
VEHICLE_429_MAX_BACKOFF_SECONDS = 180
MAX_TRIP_UPDATE_DELAY_SECONDS = 30 * 60
LINES_CACHE_SECONDS = 60 * 60
GTFS_CACHE_SECONDS = 6 * 60 * 60
TRIP_UPDATES_CACHE_SECONDS = 15
STOPS_CACHE_SECONDS = 6 * 60 * 60
TC_INFOS_CACHE_SECONDS = 6 * 60 * 60
STOPS_CACHE_FILE = Path(__file__).resolve().parent / "data" / "stops_cache.json"
PARIS_TZ = ZoneInfo("Europe/Paris")

app = Flask(__name__)
http = requests.Session()
http.headers.update({"User-Agent": "TAM-Tram-Live/1.0 (local open-data map)"})

_lines_lock = threading.Lock()
_bus_lines_lock = threading.Lock()
_bustram_lines_lock = threading.Lock()
_vehicle_lock = threading.Lock()
_gtfs_lock = threading.Lock()
_trip_updates_lock = threading.Lock()
_stops_lock = threading.Lock()
_schedule_db_lock = threading.Lock()
_vehicle_cache: tuple[float, dict] | None = None
_vehicle_retry_after = 0.0
_vehicle_retry_delay = 0
_lines_cache: tuple[float, dict] | None = None
_bus_lines_cache: tuple[float, dict] | None = None
_bustram_lines_cache: tuple[float, dict] | None = None
_gtfs_cache: tuple[float, dict] | None = None
_trip_updates_cache: tuple[float, object] | None = None
_stops_cache: tuple[float, list[dict]] | None = None
_schedule_db_path: str | None = None
_schedule_db_version: float | None = None
_schedule_db_service_date: str | None = None
_schedule_db_trip_ids: set[str] = set()
_stops_cache_until = 0.0
_tc_infos_lock = threading.Lock()
_tc_infos_cache: dict[tuple[str, str, str], tuple[float, str | None]] = {}


class _TCInfosSearchParser(HTMLParser):
    """Extract vehicle result rows from TC Infos' public search page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self._row: dict | None = None
        self._cell: dict | None = None
        self._anchor: dict | None = None

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "tr":
            self._row = {"cells": [], "anchors": [], "marks": []}
            self._cell = None
            self._anchor = None
        if self._row is None:
            return

        mark = " ".join(
            str(attributes.get(name, ""))
            for name in ("class", "title", "aria-label", "alt")
            if attributes.get(name)
        )
        if mark:
            self._row["marks"].append(mark)
            if self._cell is not None:
                self._cell["marks"].append(mark)

        if tag in {"td", "th"}:
            self._cell = {"text": [], "anchors": [], "marks": []}
            self._row["cells"].append(self._cell)
        elif tag == "a":
            self._anchor = {"href": attributes.get("href", ""), "text": []}
            self._row["anchors"].append(self._anchor)
            if self._cell is not None:
                self._cell["anchors"].append(self._anchor)

    def handle_data(self, data):
        if self._row is None:
            return
        if self._cell is not None:
            self._cell["text"].append(data)
        if self._anchor is not None:
            self._anchor["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "a":
            self._anchor = None
        elif tag in {"td", "th"}:
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(self._row)
            self._row = None
            self._cell = None
            self._anchor = None


def _normalized_tc_text(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _tc_infos_search_url(number: str) -> str:
    from urllib.parse import urlencode

    query = urlencode({
        "mode": "id", "numero": number, "immatriculation": "", "vin": "",
        "constructeur": "", "modele": "", "sort": "numero",
    })
    return f"https://tc-infos.fr/recherche?{query}"


def _resolve_tc_infos_vehicle(number: str, mode: str, line: str) -> str | None:
    """Resolve a fleet number to the TaM vehicle profile, if unambiguous."""
    cache_key = (number.casefold(), mode, line.casefold())
    now = time.monotonic()
    with _tc_infos_lock:
        cached = _tc_infos_cache.get(cache_key)
        cache_ttl = TC_INFOS_CACHE_SECONDS if cached and cached[1] else 5 * 60
        if cached and now - cached[0] < cache_ttl:
            return cached[1]

        profile_url = None
        parsed_rows = 0
        matched_profiles = 0
        try:
            response = http.get(
                _tc_infos_search_url(number),
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
                    "Referer": "https://tc-infos.fr/",
                },
                timeout=(5, 20),
            )
            response.raise_for_status()
            parser = _TCInfosSearchParser()
            parser.feed(response.text)
            parsed_rows = len(parser.rows)

            wanted_number = _normalized_tc_text(number).split()
            normalized_line = _normalized_tc_text(line)
            tram_line_number = re.search(r"\d+", normalized_line) if mode == "tramway" else None
            wanted_line_token = (
                tram_line_number.group(0)
                if tram_line_number
                else (normalized_line.split()[-1] if normalized_line else "")
            )
            candidates = []

            for row in parser.rows:
                cells = row["cells"]
                if len(cells) < 7:
                    continue
                cell_text = [" ".join(cell["text"]) for cell in cells]
                assignment = _normalized_tc_text(cell_text[6])
                if "tam" not in assignment or "montpellier" not in assignment:
                    continue

                model = _normalized_tc_text(cell_text[3])
                marks = _normalized_tc_text(" ".join(row["marks"]))
                is_tram = any(word in model.split() for word in ("citadis", "tfs", "urbos", "tramway"))
                is_tram = is_tram or "tram" in marks
                if (mode == "tramway") != is_tram:
                    continue

                vehicle_links = [
                    anchor for anchor in row["anchors"]
                    if re.fullmatch(
                        r"(?:https?://(?:www\.)?tc-infos\.fr)?/?vehicule/\d+/?",
                        re.split(r"[?#]", anchor.get("href", ""), maxsplit=1)[0],
                    )
                ]
                matched_links = []
                for anchor in vehicle_links:
                    displayed_tokens = _normalized_tc_text(" ".join(anchor["text"])).split()
                    if wanted_number and all(token in displayed_tokens for token in wanted_number):
                        matched_links.append(anchor)
                if not matched_links:
                    continue
                matched_profiles += 1

                profile_id = re.search(r"/vehicule/(\d+)", matched_links[0]["href"])
                if not profile_id:
                    continue
                score = 0
                displayed = _normalized_tc_text(" ".join(matched_links[0]["text"]))
                if displayed == _normalized_tc_text(number):
                    score += 3
                status = _normalized_tc_text(cell_text[-1])
                if "en service" in status:
                    score += 4
                if wanted_line_token:
                    row_line_tokens = _normalized_tc_text(" ".join(
                        " ".join(anchor["text"])
                        for cell in cells
                        for anchor in cell["anchors"]
                    )).split()
                    if wanted_line_token in row_line_tokens:
                        score += 8
                candidates.append((score, f"https://tc-infos.fr/vehicule/{profile_id.group(1)}"))

            if candidates:
                best_score = max(score for score, _url in candidates)
                best_urls = {url for score, url in candidates if score == best_score}
                if len(best_urls) == 1:
                    profile_url = best_urls.pop()
        except (requests.RequestException, ValueError) as exc:
            app.logger.warning("Impossible de résoudre la fiche TC Infos pour %s : %s", number, exc)

        if not profile_url:
            app.logger.warning(
                "Résolution TC Infos sans fiche directe pour %s (%s, ligne %s ; %s lignes, %s profils TaM compatibles).",
                number, mode, line or "?", parsed_rows, matched_profiles,
            )

        _tc_infos_cache[cache_key] = (time.monotonic(), profile_url)
        if len(_tc_infos_cache) > 512:
            expired_before = time.monotonic() - TC_INFOS_CACHE_SECONDS
            for key, (saved_at, _url) in list(_tc_infos_cache.items()):
                if saved_at < expired_before:
                    _tc_infos_cache.pop(key, None)
        return profile_url


def _fetch_lines() -> dict:
    """Load and cache the tram line GeoJSON shipped with the app."""
    global _lines_cache
    now = time.monotonic()
    with _lines_lock:
        if _lines_cache and now - _lines_cache[0] < LINES_CACHE_SECONDS:
            return _lines_cache[1]

        with TRAM_LINES_FILE.open("r", encoding="utf-8-sig") as source:
            geojson = json.load(source)
        if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection" or not isinstance(
            geojson.get("features"), list
        ):
            raise ValueError("Le fichier des lignes n'est pas un GeoJSON FeatureCollection valide.")
        _lines_cache = (time.monotonic(), geojson)
        return geojson


def _fetch_bus_lines() -> dict:
    """Load and cache the bus line GeoJSON shipped with the app."""
    global _bus_lines_cache
    now = time.monotonic()
    with _bus_lines_lock:
        if _bus_lines_cache and now - _bus_lines_cache[0] < LINES_CACHE_SECONDS:
            return _bus_lines_cache[1]

        with BUS_LINES_FILE.open("r", encoding="utf-8-sig") as source:
            geojson = json.load(source)
        if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection" or not isinstance(
            geojson.get("features"), list
        ):
            raise ValueError("Le fichier des lignes de bus n'est pas un GeoJSON FeatureCollection valide.")
        _bus_lines_cache = (time.monotonic(), geojson)
        return geojson


def _fetch_bustram_lines() -> dict:
    """Load and cache the Bustram A GeoJSON shipped with the app."""
    global _bustram_lines_cache
    now = time.monotonic()
    with _bustram_lines_lock:
        if _bustram_lines_cache and now - _bustram_lines_cache[0] < LINES_CACHE_SECONDS:
            return _bustram_lines_cache[1]

        with BUSTRAM_LINES_FILE.open("r", encoding="utf-8-sig") as source:
            geojson = json.load(source)
        if not isinstance(geojson, dict) or geojson.get("type") != "FeatureCollection" or not isinstance(
            geojson.get("features"), list
        ):
            raise ValueError("Le fichier du Bustram A n'est pas un GeoJSON FeatureCollection valide.")
        _bustram_lines_cache = (time.monotonic(), geojson)
        return geojson


def _gtfs_mode(route_type: int) -> str:
    if route_type == 0 or 900 <= route_type < 1000:
        return "tramway"
    if route_type == 3 or 700 <= route_type < 800:
        return "bus"
    return "autre"


def _fetch_gtfs_data() -> dict:
    """Download and cache the small GTFS tables needed by the map and schedules."""
    global _gtfs_cache
    now = time.monotonic()
    with _gtfs_lock:
        if _gtfs_cache and now - _gtfs_cache[0] < GTFS_CACHE_SECONDS:
            return _gtfs_cache[1]

        response = http.get(GTFS_URL, timeout=(5, 30))
        response.raise_for_status()
        try:
            archive = zipfile.ZipFile(io.BytesIO(response.content))
        except zipfile.BadZipFile as exc:
            raise ValueError("Le fichier GTFS reçu n'est pas une archive ZIP valide.") from exc

        members = {name.rsplit("/", 1)[-1]: name for name in archive.namelist()}
        if "routes.txt" not in members or "trips.txt" not in members:
            raise ValueError("Le GTFS ne contient pas routes.txt et trips.txt.")

        routes_by_id: dict[str, dict] = {}
        with archive.open(members["routes.txt"]) as binary, io.TextIOWrapper(
            binary, encoding="utf-8-sig", newline=""
        ) as text:
            for row in csv.DictReader(text):
                route_id = row.get("route_id", "")
                if not route_id:
                    continue
                try:
                    route_type = int(row.get("route_type", "-1"))
                except (TypeError, ValueError):
                    route_type = -1
                color = row.get("route_color", "").strip().lstrip("#")
                text_color = row.get("route_text_color", "").strip().lstrip("#")
                routes_by_id[route_id] = {
                    "route_id": route_id,
                    "short_name": row.get("route_short_name", "").strip(),
                    "long_name": row.get("route_long_name", "").strip(),
                    "route_type": route_type,
                    "mode": _gtfs_mode(route_type),
                    "color": f"#{color}" if len(color) == 6 and all(c in "0123456789abcdefABCDEF" for c in color) else "",
                    "text_color": f"#{text_color}" if len(text_color) == 6 and all(c in "0123456789abcdefABCDEF" for c in text_color) else "",
                }

        trips_by_id: dict[str, dict] = {}
        with archive.open(members["trips.txt"]) as binary, io.TextIOWrapper(
            binary, encoding="utf-8-sig", newline=""
        ) as text:
            for row in csv.DictReader(text):
                trip_id = row.get("trip_id", "")
                if trip_id:
                    trips_by_id[trip_id] = {
                        "route_id": row.get("route_id", ""),
                        "service_id": row.get("service_id", ""),
                        "headsign": row.get("trip_headsign", "").strip(),
                        "direction_id": row.get("direction_id", ""),
                    }

        stops_by_id: dict[str, dict] = {}
        if "stops.txt" in members:
            with archive.open(members["stops.txt"]) as binary, io.TextIOWrapper(
                binary, encoding="utf-8-sig", newline=""
            ) as text:
                for row in csv.DictReader(text):
                    stop_id = row.get("stop_id", "")
                    try:
                        latitude = float(row.get("stop_lat", ""))
                        longitude = float(row.get("stop_lon", ""))
                    except (TypeError, ValueError):
                        continue
                    if stop_id and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                        stops_by_id[stop_id] = {
                            "stop_id": stop_id,
                            "name": row.get("stop_name", "").strip(),
                            "latitude": latitude,
                            "longitude": longitude,
                            "parent_station": row.get("parent_station", "").strip(),
                        }

        calendar_by_service: dict[str, dict] = {}
        if "calendar.txt" in members:
            with archive.open(members["calendar.txt"]) as binary, io.TextIOWrapper(
                binary, encoding="utf-8-sig", newline=""
            ) as text:
                for row in csv.DictReader(text):
                    service_id = row.get("service_id", "")
                    if not service_id:
                        continue
                    try:
                        start_date = datetime.strptime(row.get("start_date", ""), "%Y%m%d").date()
                        end_date = datetime.strptime(row.get("end_date", ""), "%Y%m%d").date()
                    except ValueError:
                        continue
                    calendar_by_service[service_id] = {
                        "start_date": start_date,
                        "end_date": end_date,
                        "weekdays": tuple(row.get(day, "0") == "1" for day in (
                            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"
                        )),
                    }

        calendar_dates: dict[str, list[tuple[str, int]]] = {}
        if "calendar_dates.txt" in members:
            with archive.open(members["calendar_dates.txt"]) as binary, io.TextIOWrapper(
                binary, encoding="utf-8-sig", newline=""
            ) as text:
                for row in csv.DictReader(text):
                    date_key = row.get("date", "")
                    service_id = row.get("service_id", "")
                    try:
                        exception_type = int(row.get("exception_type", "0"))
                        datetime.strptime(date_key, "%Y%m%d")
                    except ValueError:
                        continue
                    if service_id and exception_type in (1, 2):
                        calendar_dates.setdefault(date_key, []).append((service_id, exception_type))

        data = {
            "routes_by_id": routes_by_id,
            "trips_by_id": trips_by_id,
            "stops_by_id": stops_by_id,
            "calendar_by_service": calendar_by_service,
            "calendar_dates": calendar_dates,
            "archive_content": response.content,
            "archive_members": members,
        }
        _gtfs_cache = (time.monotonic(), data)
        return data


def _active_service_ids(data: dict, service_date) -> set[str]:
    """Resolve the GTFS services operating on one local service date."""
    active = set()
    weekday = service_date.weekday()
    for service_id, calendar in data.get("calendar_by_service", {}).items():
        if (calendar["start_date"] <= service_date <= calendar["end_date"]
                and calendar["weekdays"][weekday]):
            active.add(service_id)
    date_key = service_date.strftime("%Y%m%d")
    for service_id, exception_type in data.get("calendar_dates", {}).get(date_key, []):
        if exception_type == 1:
            active.add(service_id)
        else:
            active.discard(service_id)
    return active


def _service_date_now():
    """TAM schedules can run past midnight; keep those trips on yesterday's service day."""
    current = datetime.now(PARIS_TZ)
    return (current - timedelta(days=1)).date() if current.hour < 3 else current.date()


def _ensure_schedule_db(data: dict, service_date) -> tuple[str | None, set[str]]:
    """Build a small indexed SQLite database containing only today's active trips."""
    global _schedule_db_path, _schedule_db_version, _schedule_db_service_date, _schedule_db_trip_ids
    cache_version = _gtfs_cache[0] if _gtfs_cache else 0
    date_key = service_date.isoformat()
    with _schedule_db_lock:
        if (_schedule_db_path and _schedule_db_version == cache_version
                and _schedule_db_service_date == date_key and Path(_schedule_db_path).exists()):
            return _schedule_db_path, _schedule_db_trip_ids

        if _schedule_db_path:
            try:
                os.remove(_schedule_db_path)
            except OSError:
                pass
        active_services = _active_service_ids(data, service_date)
        trip_ids = {
            trip_id for trip_id, trip in data["trips_by_id"].items()
            if trip.get("service_id") in active_services
        }
        members = data.get("archive_members", {})
        if not trip_ids or "stop_times.txt" not in members:
            _schedule_db_path = None
            _schedule_db_trip_ids = trip_ids
            _schedule_db_version = cache_version
            _schedule_db_service_date = date_key
            return None, trip_ids

        descriptor, db_path = tempfile.mkstemp(prefix="tam-schedule-", suffix=".sqlite3")
        os.close(descriptor)
        try:
            archive = zipfile.ZipFile(io.BytesIO(data["archive_content"]))
            connection = sqlite3.connect(db_path)
            connection.execute("PRAGMA journal_mode=OFF")
            connection.execute("PRAGMA synchronous=OFF")
            connection.execute("CREATE TABLE stop_times (stop_id TEXT, trip_id TEXT, arrival_time TEXT, departure_time TEXT, stop_sequence INTEGER)")
            batch = []
            with archive.open(members["stop_times.txt"]) as binary, io.TextIOWrapper(
                binary, encoding="utf-8-sig", newline=""
            ) as text:
                for row in csv.DictReader(text):
                    trip_id = row.get("trip_id", "")
                    if trip_id not in trip_ids:
                        continue
                    try:
                        sequence = int(row.get("stop_sequence", "0"))
                    except (TypeError, ValueError):
                        sequence = 0
                    batch.append((
                        row.get("stop_id", ""), trip_id,
                        row.get("arrival_time", ""), row.get("departure_time", ""), sequence,
                    ))
                    if len(batch) >= 10000:
                        connection.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)", batch)
                        batch.clear()
            if batch:
                connection.executemany("INSERT INTO stop_times VALUES (?, ?, ?, ?, ?)", batch)
            connection.execute("CREATE INDEX stop_times_by_stop ON stop_times(stop_id)")
            connection.execute("CREATE INDEX stop_times_by_trip_seq ON stop_times(trip_id, stop_sequence)")
            connection.commit()
            connection.close()
            archive.close()
        except Exception:
            try:
                os.remove(db_path)
            except OSError:
                pass
            raise

        _schedule_db_path = db_path
        _schedule_db_trip_ids = trip_ids
        _schedule_db_version = cache_version
        _schedule_db_service_date = date_key
        return db_path, trip_ids


def _normalized_name(value: str) -> str:
    value = unicodedata.normalize("NFKD", str(value or "")).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


def _trip_headsign(trip: dict, route: dict) -> str:
    headsign = str(trip.get("headsign", "")).strip()
    if headsign and _normalized_name(headsign) == _normalized_name(route.get("long_name", "")):
        return ""
    return headsign


def _gtfs_stop_candidates(name: str, latitude: float, longitude: float, stops_by_id: dict) -> list[str]:
    normalized = _normalized_name(name)
    tokens = set(normalized.split())
    candidates = []
    for stop_id, stop in stops_by_id.items():
        lat_delta = (stop["latitude"] - latitude) * 111_320
        lon_delta = (stop["longitude"] - longitude) * 111_320 * math.cos(math.radians(latitude))
        distance = math.hypot(lat_delta, lon_delta)
        if distance > 180:
            continue
        gtfs_name = _normalized_name(stop.get("name", ""))
        if not gtfs_name:
            continue
        gtfs_tokens = set(gtfs_name.split())
        overlap = len(tokens & gtfs_tokens) / max(1, len(tokens | gtfs_tokens))
        substring_match = normalized in gtfs_name or gtfs_name in normalized
        similarity = difflib.SequenceMatcher(None, normalized, gtfs_name).ratio()
        if substring_match or overlap >= 0.45 or similarity >= 0.68:
            candidates.append((-int(substring_match), -max(overlap, similarity), distance, stop_id))
    candidates.sort()
    # Prefer matching names over slightly closer, unrelated platforms around an interchange.
    return [item[3] for item in candidates if item[2] <= 130][:100]


def _stop_distance_meters(first: dict, second: dict) -> float:
    latitude = (first["latitude"] + second["latitude"]) / 2
    north = (first["latitude"] - second["latitude"]) * 111_320
    east = (first["longitude"] - second["longitude"]) * 111_320 * math.cos(math.radians(latitude))
    return math.hypot(north, east)


def _merge_platforms(stops: list[dict]) -> list[dict]:
    """Combine nearby directional platforms into one clickable stop."""
    buckets: dict[str, list[dict]] = {}
    merged = []
    for stop in stops:
        key = _normalized_name(stop["name"])
        candidates = buckets.setdefault(key, [])
        group = next((item for item in candidates if _stop_distance_meters(item, stop) <= 150), None)
        point = {"name": stop["name"], "latitude": stop["latitude"], "longitude": stop["longitude"]}
        if group is None:
            group = {
                **stop,
                "schedule_match_points": [point],
                "gtfs_stop_ids": list(stop.get("gtfs_stop_ids", [])),
                "_latitudes": [stop["latitude"]],
                "_longitudes": [stop["longitude"]],
            }
            candidates.append(group)
            merged.append(group)
        else:
            group["schedule_match_points"].append(point)
            group["gtfs_stop_ids"] = list(dict.fromkeys(
                group["gtfs_stop_ids"] + stop.get("gtfs_stop_ids", [])
            ))[:100]
            group["_latitudes"].append(stop["latitude"])
            group["_longitudes"].append(stop["longitude"])

    for group in merged:
        group["latitude"] = sum(group.pop("_latitudes")) / len(group["schedule_match_points"])
        group["longitude"] = sum(group.pop("_longitudes")) / len(group["schedule_match_points"])
    return merged


def _stops_from_gtfs(gtfs: dict) -> dict[str, list[dict]]:
    """Build a stop fallback from GTFS when the separate map feeds are unavailable."""
    trip_modes = {
        trip_id: gtfs["routes_by_id"].get(trip.get("route_id", ""), {}).get("mode")
        for trip_id, trip in gtfs["trips_by_id"].items()
    }
    modes_by_stop: dict[str, set[str]] = {}
    members = gtfs.get("archive_members", {})
    if "stop_times.txt" not in members:
        return {"tramway": [], "bus": []}
    with zipfile.ZipFile(io.BytesIO(gtfs["archive_content"])) as archive:
        with archive.open(members["stop_times.txt"]) as binary, io.TextIOWrapper(
            binary, encoding="utf-8-sig", newline=""
        ) as text:
            for row in csv.DictReader(text):
                mode = trip_modes.get(row.get("trip_id", ""))
                stop_id = row.get("stop_id", "")
                if mode not in {"tramway", "bus"} or not stop_id:
                    continue
                modes_by_stop.setdefault(stop_id, set()).add(mode)

    fallback = {"tramway": [], "bus": []}
    for stop_id, modes in modes_by_stop.items():
        stop = gtfs["stops_by_id"].get(stop_id)
        if not stop or not stop.get("name"):
            continue
        for mode in modes:
            fallback[mode].append({
                "id": f"gtfs:{mode}:{stop_id}",
                "name": stop["name"],
                "mode": mode,
                "latitude": stop["latitude"],
                "longitude": stop["longitude"],
                "gtfs_stop_ids": [stop_id],
            })
    return {mode: _merge_platforms(items) for mode, items in fallback.items()}


def _read_stops_disk_cache() -> list[dict]:
    try:
        with STOPS_CACHE_FILE.open("r", encoding="utf-8") as source:
            cached = json.load(source)
        if isinstance(cached, list):
            valid = [
                stop for stop in cached
                if isinstance(stop, dict)
                and stop.get("mode") in {"tramway", "bus"}
                and stop.get("name")
                and isinstance(stop.get("latitude"), (int, float))
                and isinstance(stop.get("longitude"), (int, float))
            ]
            merged = []
            for mode in ("tramway", "bus"):
                mode_stops = [stop for stop in valid if stop.get("mode") == mode]
                if mode_stops and all("schedule_match_points" in stop for stop in mode_stops):
                    merged.extend(mode_stops)
                elif mode_stops:
                    merged.extend(_merge_platforms(mode_stops))
            return merged
    except (OSError, ValueError, TypeError):
        pass
    return []


def _write_stops_disk_cache(stops: list[dict]) -> None:
    try:
        temporary = STOPS_CACHE_FILE.with_suffix(".tmp")
        temporary.write_text(json.dumps(stops, ensure_ascii=False), encoding="utf-8")
        temporary.replace(STOPS_CACHE_FILE)
    except OSError as exc:
        app.logger.info("Le cache local des arrêts n'a pas pu être enregistré : %s", exc)


def _load_stop_feed(mode: str, url: str, gtfs: dict | None) -> list[dict]:
    response = http.get(url, timeout=(3, 12))
    response.raise_for_status()
    geojson = response.json()
    features = geojson.get("features") if isinstance(geojson, dict) else None
    if not isinstance(features, list) or not features:
        raise ValueError("Le flux GeoJSON ne contient pas d'arrêts.")
    stops = []
    for feature in features:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point":
            continue
        coordinates = geometry.get("coordinates") or []
        if len(coordinates) < 2:
            continue
        try:
            longitude, latitude = float(coordinates[0]), float(coordinates[1])
        except (TypeError, ValueError):
            continue
        properties = feature.get("properties") or {}
        name = str(properties.get("description") or properties.get("name") or properties.get("nom") or "").strip()
        name = re.sub(r"<[^>]*>", "", name)
        if not name or not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        candidates = _gtfs_stop_candidates(name, latitude, longitude, gtfs["stops_by_id"]) if gtfs else []
        stops.append({
            "id": f"{mode}:{feature.get('id') or len(stops)}",
            "name": name,
            "mode": mode,
            "latitude": latitude,
            "longitude": longitude,
            "gtfs_stop_ids": candidates,
        })
    if not stops:
        raise ValueError("Le flux ne contient aucun arrêt exploitable.")
    return _merge_platforms(stops)


def _fetch_stops() -> list[dict]:
    """Load the independent stop map feeds without blocking on the GTFS download."""
    global _stops_cache, _stops_cache_until
    now = time.monotonic()
    with _stops_lock:
        if _stops_cache and now < _stops_cache_until:
            return _stops_cache[1]

        previous = _stops_cache[1] if _stops_cache else _read_stops_disk_cache()
        previous_by_mode = {
            mode: [stop for stop in previous if stop.get("mode") == mode]
            for mode in ("tramway", "bus")
        }
        errors = []
        # The map only needs names and coordinates. GTFS stop matching is lazy,
        # when someone opens a stop's schedule, so a slow GTFS server cannot
        # prevent the stop markers from appearing.
        gtfs = _cached_gtfs_data()

        stops_by_mode: dict[str, list[dict]] = {"tramway": [], "bus": []}
        feeds = {"tramway": TRAM_STOPS_URL, "bus": BUS_STOPS_URL}
        with ThreadPoolExecutor(max_workers=2) as executor:
            requests_by_future = {
                executor.submit(_load_stop_feed, mode, url, gtfs): mode
                for mode, url in feeds.items()
            }
            for future in as_completed(requests_by_future):
                mode = requests_by_future[future]
                try:
                    stops_by_mode[mode] = future.result()
                except (requests.RequestException, ValueError, TypeError, AttributeError) as exc:
                    errors.append(f"{mode}: {exc}")
                    app.logger.warning("Impossible de charger les arrêts %s: %s", mode, exc)
                    stops_by_mode[mode] = previous_by_mode[mode]

        if gtfs and any(not stops_by_mode[mode] for mode in ("tramway", "bus")):
            try:
                fallback = _stops_from_gtfs(gtfs)
                for mode in ("tramway", "bus"):
                    if not stops_by_mode[mode]:
                        stops_by_mode[mode] = fallback[mode]
            except (OSError, ValueError, zipfile.BadZipFile) as exc:
                errors.append(f"GTFS stops: {exc}")
                app.logger.warning("Impossible de créer les arrêts de secours depuis le GTFS: %s", exc)

        stops = stops_by_mode["tramway"] + stops_by_mode["bus"]
        if not stops:
            raise ValueError("Le service de données des arrêts est temporairement indisponible.")

        _stops_cache = (time.monotonic(), stops)
        _stops_cache_until = time.monotonic() + (300 if errors else STOPS_CACHE_SECONDS)
        _write_stops_disk_cache(stops)
    return stops


def _gtfs_time_datetime(service_date, value: str):
    match = re.fullmatch(r"(\d+):(\d{2}):(\d{2})", str(value or ""))
    if not match:
        return None
    hours, minutes, seconds = map(int, match.groups())
    return datetime.combine(service_date, datetime.min.time(), tzinfo=PARIS_TZ) + timedelta(
        hours=hours, minutes=minutes, seconds=seconds
    )


def _fetch_trip_updates():
    global _trip_updates_cache
    now = time.monotonic()
    with _trip_updates_lock:
        if _trip_updates_cache and now - _trip_updates_cache[0] < TRIP_UPDATES_CACHE_SECONDS:
            return _trip_updates_cache[1]
        response = http.get(TRIP_UPDATES_URL, timeout=12)
        response.raise_for_status()
        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(response.content)
        except DecodeError as exc:
            raise ValueError("Le flux des horaires temps réel n'est pas lisible.") from exc
        _trip_updates_cache = (time.monotonic(), feed)
        return feed


def _scheduled_stop_time(connection, trip_id: str, stop_id: str, stop_sequence: int | None):
    row = None
    if stop_id:
        row = connection.execute(
            "SELECT stop_id, trip_id, arrival_time, departure_time, stop_sequence FROM stop_times WHERE trip_id=? AND stop_id=? ORDER BY stop_sequence LIMIT 1",
            (trip_id, stop_id),
        ).fetchone()
    if row is None and stop_sequence is not None:
        row = connection.execute(
            "SELECT stop_id, trip_id, arrival_time, departure_time, stop_sequence FROM stop_times WHERE trip_id=? AND stop_sequence=? LIMIT 1",
            (trip_id, stop_sequence),
        ).fetchone()
    return row


def _stop_time_row_for_update(connection, trip_id: str, stop_update):
    if connection is None or not trip_id:
        return None
    stop_sequence = int(stop_update.stop_sequence) if stop_update.HasField("stop_sequence") else None
    stop_id = stop_update.stop_id if stop_update.HasField("stop_id") else ""
    if stop_sequence is not None:
        # A stop can occur more than once on a trip. If RT supplied a sequence,
        # never guess another occurrence by stop_id when that sequence is absent.
        row = _scheduled_stop_time(connection, trip_id, "", stop_sequence)
        if row is None:
            return None
        if stop_id and row[0] != stop_id:
            return None
        return row
    return _scheduled_stop_time(connection, trip_id, stop_id, None) if stop_id else None


def _realtime_event_time_and_delay(event, scheduled_time):
    event_time = datetime.fromtimestamp(event.time, PARIS_TZ) if event.HasField("time") else None
    reported_delay = int(event.delay) if event.HasField("delay") else None
    if event_time is None and reported_delay is not None and scheduled_time is not None:
        event_time = scheduled_time + timedelta(seconds=reported_delay)

    if event_time is not None and scheduled_time is not None:
        delay = round((event_time - scheduled_time).total_seconds())
    else:
        delay = reported_delay
    if delay is not None and abs(delay) > MAX_TRIP_UPDATE_DELAY_SECONDS:
        return None, None
    return event_time, delay


def _schedule_departures(stop_ids: list[str], mode: str, tab: str) -> dict:
    gtfs = _fetch_gtfs_data()
    service_date = _service_date_now()
    db_path, active_trip_ids = _ensure_schedule_db(gtfs, service_date)
    now = datetime.now(PARIS_TZ)
    routes = gtfs["routes_by_id"]
    trips = gtfs["trips_by_id"]
    stop_id_set = set(stop_ids)
    departures = []

    if tab == "theoretical":
        if not db_path or not stop_id_set:
            return {"departures": [], "service_date": service_date.isoformat(), "updated_at": None}
        placeholders = ",".join("?" for _ in stop_id_set)
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                f"SELECT stop_id, trip_id, arrival_time, departure_time FROM stop_times WHERE stop_id IN ({placeholders})",
                tuple(stop_id_set),
            ).fetchall()
        for stop_id, trip_id, arrival_time, departure_time in rows:
            trip = trips.get(trip_id, {})
            route = routes.get(trip.get("route_id", ""), {})
            if route.get("mode") != mode:
                continue
            scheduled = _gtfs_time_datetime(service_date, departure_time or arrival_time)
            if not scheduled or scheduled < now - timedelta(minutes=1) or scheduled > now + timedelta(hours=12):
                continue
            departures.append({
                "timestamp": scheduled.timestamp(),
                "route": route.get("short_name") or route.get("long_name") or "?",
                "color": route.get("color") or ("#0876b9" if mode == "tramway" else "#19785e"),
                "direction": _trip_headsign(trip, route),
                "vehicle": "",
                "delay": None,
                "realtime": False,
            })
    else:
        feed = _fetch_trip_updates()
        connection = sqlite3.connect(db_path) if db_path else None
        try:
            for entity in feed.entity:
                if not entity.HasField("trip_update"):
                    continue
                update = entity.trip_update
                if (update.HasField("trip") and update.trip.HasField("schedule_relationship")
                        and int(update.trip.schedule_relationship) == 3):
                    continue
                trip_id = update.trip.trip_id if update.HasField("trip") else ""
                trip = trips.get(trip_id, {})
                route_id = (update.trip.route_id if update.HasField("trip") else "") or trip.get("route_id", "")
                route = routes.get(route_id, {})
                if route.get("mode") != mode:
                    continue
                if active_trip_ids and trip_id and trip_id not in active_trip_ids:
                    continue
                for stop_update in update.stop_time_update:
                    if stop_update.HasField("schedule_relationship") and int(stop_update.schedule_relationship) in (1, 2):
                        continue
                    stop_id = stop_update.stop_id if stop_update.HasField("stop_id") else ""
                    scheduled_row = _stop_time_row_for_update(connection, trip_id, stop_update)
                    if scheduled_row:
                        stop_id = scheduled_row[0]
                    if stop_id not in stop_id_set:
                        continue
                    is_arrival = stop_update.HasField("arrival")
                    event = stop_update.arrival if is_arrival else (
                        stop_update.departure if stop_update.HasField("departure") else None
                    )
                    if event is None:
                        continue
                    scheduled = None
                    if scheduled_row:
                        scheduled_value = scheduled_row[2] if is_arrival else scheduled_row[3]
                        scheduled = _gtfs_time_datetime(service_date, scheduled_value or (scheduled_row[3] if is_arrival else scheduled_row[2]))
                    event_time, delay_seconds = _realtime_event_time_and_delay(event, scheduled)
                    if not event_time or event_time < now - timedelta(minutes=1) or event_time > now + timedelta(hours=8):
                        continue
                    vehicle = ""
                    if update.HasField("vehicle"):
                        vehicle = update.vehicle.label or update.vehicle.id
                    departures.append({
                        "timestamp": event_time.timestamp(),
                        "route": route.get("short_name") or route.get("long_name") or "?",
                        "color": route.get("color") or ("#0876b9" if mode == "tramway" else "#19785e"),
                        "direction": _trip_headsign(trip, route),
                        "vehicle": vehicle,
                        "delay": delay_seconds,
                        "realtime": True,
                    })
        finally:
            if connection:
                connection.close()

    departures.sort(key=lambda item: item["timestamp"])
    unique = []
    seen = set()
    for item in departures:
        key = (item["timestamp"], item["route"], item["direction"])
        if key in seen:
            continue
        seen.add(key)
        item["time"] = datetime.fromtimestamp(item["timestamp"], PARIS_TZ).isoformat()
        item["minutes"] = max(0, round((item["timestamp"] - now.timestamp()) / 60))
        unique.append(item)
        if len(unique) >= 24:
            break
    return {
        "departures": unique,
        "service_date": service_date.isoformat(),
        "updated_at": datetime.now(timezone.utc).isoformat() if tab == "realtime" else None,
    }


def _trip_journey(trip_id: str, current_stop_sequence: int | None = None,
                  current_status: int | None = None, current_stop_id: str = "") -> dict:
    gtfs = _fetch_gtfs_data()
    trip = gtfs["trips_by_id"].get(trip_id)
    if not trip:
        raise KeyError("Ce trajet n’est plus présent dans les horaires du réseau.")
    route = gtfs["routes_by_id"].get(trip.get("route_id", ""), {})
    mode = route.get("mode", "")
    if mode not in {"tramway", "bus"}:
        raise KeyError("Le parcours de ce véhicule n’est pas disponible.")

    service_date = _service_date_now()
    db_path, _active_trip_ids = _ensure_schedule_db(gtfs, service_date)
    rows = []
    if db_path:
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute(
                "SELECT stop_id, trip_id, arrival_time, departure_time, stop_sequence "
                "FROM stop_times WHERE trip_id=? ORDER BY stop_sequence",
                (trip_id,),
            ).fetchall()

    realtime_updates = {}
    if rows:
        try:
            feed = _fetch_trip_updates()
        except (requests.RequestException, ValueError) as exc:
            app.logger.info("Horaires temps réel indisponibles pour le trajet %s: %s", trip_id, exc)
            feed = None
        if feed is not None:
            for entity in feed.entity:
                if not entity.HasField("trip_update"):
                    continue
                update = entity.trip_update
                if not update.HasField("trip") or update.trip.trip_id != trip_id:
                    continue
                if update.trip.HasField("schedule_relationship") and int(update.trip.schedule_relationship) == 3:
                    break
                for stop_update in update.stop_time_update:
                    if stop_update.HasField("schedule_relationship") and int(stop_update.schedule_relationship) in (1, 2):
                        continue
                    sequence = int(stop_update.stop_sequence) if stop_update.HasField("stop_sequence") else None
                    stop_id = stop_update.stop_id if stop_update.HasField("stop_id") else ""
                    if sequence is not None:
                        realtime_updates[("sequence", sequence)] = stop_update
                    elif stop_id:
                        realtime_updates[("stop_id", stop_id)] = stop_update

    now = datetime.now(PARIS_TZ)
    stops = []
    for stop_id, _row_trip_id, arrival_time, departure_time, sequence in rows:
        stop = gtfs["stops_by_id"].get(stop_id, {})
        stop_update = realtime_updates.get(("sequence", int(sequence)))
        if (stop_update is not None and stop_update.HasField("stop_id")
                and stop_update.stop_id != stop_id):
            stop_update = None
        if stop_update is None:
            stop_update = realtime_updates.get(("stop_id", stop_id))
        event = None
        if stop_update is not None:
            event = stop_update.arrival if stop_update.HasField("arrival") else (
                stop_update.departure if stop_update.HasField("departure") else None
            )
        use_departure_time = bool(
            event is not None and stop_update is not None
            and not stop_update.HasField("arrival") and stop_update.HasField("departure")
        )
        scheduled_value = (departure_time or arrival_time) if use_departure_time else (arrival_time or departure_time)
        scheduled = _gtfs_time_datetime(service_date, scheduled_value)
        estimated, delay = _realtime_event_time_and_delay(event, scheduled) if event is not None else (None, None)
        shown_time = estimated or scheduled
        if shown_time is None:
            continue
        stops.append({
            "stop_id": stop_id,
            "name": stop.get("name") or stop_id,
            "sequence": int(sequence),
            "scheduled_time": scheduled.isoformat() if scheduled else None,
            "time": shown_time.isoformat(),
            "minutes": max(0, round((shown_time - now).total_seconds() / 60)),
            "delay": delay,
            "realtime": estimated is not None,
        })

    if current_stop_sequence is None and current_stop_id:
        matching = next((item for item in stops if item["stop_id"] == current_stop_id), None)
        if matching:
            current_stop_sequence = matching["sequence"]
    if current_stop_sequence is None:
        current_stop_sequence = next(
            (item["sequence"] for item in stops if datetime.fromisoformat(item["time"]) >= now),
            stops[-1]["sequence"] if stops else None,
        )

    current_index = next(
        (index for index, item in enumerate(stops) if item["sequence"] == current_stop_sequence),
        None,
    )
    if current_index is None and current_stop_id:
        current_index = next(
            (index for index, item in enumerate(stops) if item["stop_id"] == current_stop_id),
            None,
        )
    if current_index is None and current_stop_sequence is not None:
        current_index = next(
            (index for index, item in enumerate(stops) if item["sequence"] >= current_stop_sequence),
            len(stops) - 1 if stops else None,
        )
    for index, item in enumerate(stops):
        if current_index is None:
            item["state"] = "upcoming"
        elif index < current_index:
            item["state"] = "passed"
        elif index == current_index:
            item["state"] = "current" if current_status == 1 else "next"
        else:
            item["state"] = "upcoming"

    return {
        "trip_id": trip_id,
        "route": route.get("short_name") or route.get("long_name") or "?",
        "route_name": route.get("long_name") or "",
        "mode": mode,
        "color": route.get("color") or ("#0876b9" if mode == "tramway" else "#19785e"),
        "direction": _trip_headsign(trip, route),
        "current_status": current_status,
        "stops": stops,
    }


def _cached_gtfs_data() -> dict | None:
    with _gtfs_lock:
        if _gtfs_cache and time.monotonic() - _gtfs_cache[0] < GTFS_CACHE_SECONDS:
            return _gtfs_cache[1]
    return None


def _vehicle_to_dict(entity, gtfs_data: dict | None = None) -> dict | None:
    if not entity.HasField("vehicle"):
        return None

    vehicle = entity.vehicle
    if not vehicle.HasField("position"):
        return None

    position = vehicle.position
    latitude = position.latitude
    longitude = position.longitude
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None

    trip = vehicle.trip if vehicle.HasField("trip") else None
    vehicle_details = vehicle.vehicle if vehicle.HasField("vehicle") else None
    timestamp = int(vehicle.timestamp) if vehicle.HasField("timestamp") else None
    current_status = (
        int(vehicle.current_status) if vehicle.HasField("current_status") else None
    )

    trip_id = trip.trip_id if trip else ""
    trip_info = (gtfs_data or {}).get("trips_by_id", {}).get(trip_id, {})
    route_id = (trip.route_id if trip and trip.route_id else "") or trip_info.get("route_id", "")
    route_info = (gtfs_data or {}).get("routes_by_id", {}).get(route_id, {})

    return {
        "id": (vehicle_details.id if vehicle_details and vehicle_details.id else entity.id),
        "label": vehicle_details.label if vehicle_details else "",
        "route_id": route_id,
        "trip_id": trip_id,
        "route_short_name": route_info.get("short_name", ""),
        "route_long_name": route_info.get("long_name", ""),
        "route_type": route_info.get("route_type"),
        "route_mode": route_info.get("mode", ""),
        "route_color": route_info.get("color", ""),
        "route_text_color": route_info.get("text_color", ""),
        "direction": _trip_headsign(trip_info, route_info),
        "latitude": latitude,
        "longitude": longitude,
        "bearing": int(position.bearing) if position.HasField("bearing") else None,
        "timestamp": timestamp,
        "current_status": current_status,
        "current_stop_sequence": int(vehicle.current_stop_sequence) if vehicle.HasField("current_stop_sequence") else None,
        "stop_id": vehicle.stop_id if vehicle.HasField("stop_id") else "",
    }


def _filter_vehicle_payload(payload: dict) -> dict:
    """Drop positions that have become too old while a cached feed is reused."""
    now_timestamp = int(time.time())
    vehicles = [
        vehicle for vehicle in payload.get("vehicles", [])
        if vehicle.get("timestamp") is None
        or now_timestamp - vehicle["timestamp"] <= VEHICLE_MAX_AGE_SECONDS
    ]
    return {**payload, "vehicles": vehicles, "count": len(vehicles)}


def _empty_rate_limited_vehicle_payload() -> dict:
    return {
        "vehicles": [],
        "count": 0,
        "fetched_at": None,
        "feed_timestamp": None,
        "temporarily_limited": True,
    }


def _fetch_vehicles() -> dict:
    """Download and decode one GTFS-Realtime VehiclePositions feed."""
    global _vehicle_cache, _vehicle_retry_after, _vehicle_retry_delay
    with _vehicle_lock:
        now = time.monotonic()
        if _vehicle_cache and now - _vehicle_cache[0] < VEHICLE_CACHE_SECONDS:
            return _filter_vehicle_payload(_vehicle_cache[1])
        if now < _vehicle_retry_after:
            if _vehicle_cache:
                return _filter_vehicle_payload(_vehicle_cache[1])
            return _empty_rate_limited_vehicle_payload()

        response = http.get(VEHICLE_POSITIONS_URL, timeout=12)
        if response.status_code == 429:
            _vehicle_retry_delay = min(
                VEHICLE_429_MAX_BACKOFF_SECONDS,
                max(VEHICLE_429_MIN_BACKOFF_SECONDS, _vehicle_retry_delay * 2),
            )
            retry_after = response.headers.get("Retry-After", "")
            try:
                retry_seconds = max(0, int(float(retry_after)))
            except (TypeError, ValueError):
                retry_seconds = 0
            cooldown = min(
                VEHICLE_429_MAX_BACKOFF_SECONDS,
                max(_vehicle_retry_delay, retry_seconds),
            )
            _vehicle_retry_after = time.monotonic() + cooldown
            app.logger.info("Flux TAM limité (429); réutilisation du dernier flux et pause de %s s.", cooldown)
            if _vehicle_cache:
                return _filter_vehicle_payload(_vehicle_cache[1])
            return _empty_rate_limited_vehicle_payload()
        response.raise_for_status()

        feed = gtfs_realtime_pb2.FeedMessage()
        try:
            feed.ParseFromString(response.content)
        except DecodeError as exc:
            raise ValueError("Le flux reçu n'est pas un message GTFS-Realtime valide.") from exc

        gtfs_data = _cached_gtfs_data()
        vehicles = [
            item
            for entity in feed.entity
            if (item := _vehicle_to_dict(entity, gtfs_data)) is not None
        ]
        now_timestamp = int(time.time())
        vehicles = [
            vehicle for vehicle in vehicles
            if vehicle["timestamp"] is None
            or now_timestamp - vehicle["timestamp"] <= VEHICLE_MAX_AGE_SECONDS
        ]
        fetched_at = datetime.now(timezone.utc).isoformat()
        feed_timestamp = (
            int(feed.header.timestamp) if feed.header.HasField("timestamp") else None
        )
        payload = {
            "vehicles": vehicles,
            "count": len(vehicles),
            "fetched_at": fetched_at,
            "feed_timestamp": feed_timestamp,
        }
        _vehicle_cache = (time.monotonic(), payload)
        _vehicle_retry_after = 0.0
        _vehicle_retry_delay = 0
        return payload


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/lines")
def lines():
    try:
        return jsonify(_fetch_lines())
    except (OSError, ValueError) as exc:
        app.logger.warning("Impossible de charger les lignes du tram : %s", exc)
        return jsonify(error="Impossible de charger le tracé des lignes. Vérifiez votre connexion."), 502


@app.get("/api/bus-lines")
def bus_lines():
    try:
        return jsonify(_fetch_bus_lines())
    except (OSError, ValueError) as exc:
        app.logger.warning("Impossible de charger les lignes de bus : %s", exc)
        return jsonify(error="Impossible de charger le tracé des lignes de bus."), 502


@app.get("/api/bustram-lines")
def bustram_lines():
    try:
        return jsonify(_fetch_bustram_lines())
    except (OSError, ValueError) as exc:
        app.logger.warning("Impossible de charger le Bustram A : %s", exc)
        return jsonify(error="Impossible de charger le tracé du Bustram A."), 502


@app.get("/api/routes")
def routes():
    try:
        data = _fetch_gtfs_data()
        route_list = [
            route for route in data["routes_by_id"].values()
            if route["mode"] in {"tramway", "bus"}
        ]
        route_list.sort(key=lambda route: (route["mode"] != "tramway", route["short_name"], route["long_name"]))
        return jsonify(routes=route_list)
    except (requests.RequestException, ValueError, zipfile.BadZipFile) as exc:
        app.logger.warning("Impossible de charger les lignes GTFS : %s", exc)
        return jsonify(error="Impossible de charger les lignes et directions du réseau."), 502


@app.get("/api/stops")
def stops():
    try:
        return jsonify(stops=_fetch_stops())
    except (requests.RequestException, ValueError, zipfile.BadZipFile) as exc:
        app.logger.warning("Impossible de charger les arrêts TAM : %s", exc)
        return jsonify(error="Le service de données des arrêts est temporairement indisponible. Nouvelle tentative automatique."), 502


@app.get("/api/stop-schedules")
def stop_schedules():
    mode = request.args.get("mode", "")
    tab = request.args.get("tab", "realtime")
    if mode not in {"bus", "tramway"} or tab not in {"realtime", "theoretical"}:
        return jsonify(error="Paramètres d’horaires invalides."), 400
    requested_ids = [value for value in request.args.get("stop_ids", "").split(",") if value]
    if len(requested_ids) > 100:
        return jsonify(error="Trop d’identifiants d’arrêt."), 400
    try:
        gtfs = _fetch_gtfs_data()
        if not requested_ids:
            try:
                match_points = json.loads(request.args.get("points", "[]"))
            except (TypeError, ValueError):
                match_points = []
            if not isinstance(match_points, list):
                match_points = []
            if not match_points:
                match_points = [{
                    "name": request.args.get("name", "").strip(),
                    "latitude": request.args.get("latitude"),
                    "longitude": request.args.get("longitude"),
                }]
            for point in match_points[:20]:
                if not isinstance(point, dict):
                    continue
                try:
                    name = str(point.get("name", "")).strip()
                    latitude = float(point.get("latitude"))
                    longitude = float(point.get("longitude"))
                except (TypeError, ValueError):
                    continue
                if name and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    requested_ids.extend(_gtfs_stop_candidates(name, latitude, longitude, gtfs["stops_by_id"]))
            requested_ids = list(dict.fromkeys(requested_ids))[:100]
        stop_ids = [stop_id for stop_id in dict.fromkeys(requested_ids) if stop_id in gtfs["stops_by_id"]]
        if not stop_ids:
            return jsonify(departures=[], stop_ids=[], service_date=_service_date_now().isoformat(), updated_at=None)
        result = _schedule_departures(stop_ids, mode, tab)
        result["stop_ids"] = stop_ids
        return jsonify(result)
    except (requests.RequestException, ValueError, zipfile.BadZipFile, sqlite3.Error) as exc:
        app.logger.warning("Impossible de charger les horaires %s : %s", mode, exc)
        return jsonify(error="Impossible de charger les horaires pour cet arrêt."), 502


@app.get("/api/trip-stops")
def trip_stops():
    trip_id = request.args.get("trip_id", "").strip()
    if not trip_id or len(trip_id) > 200:
        return jsonify(error="Identifiant de trajet invalide."), 400
    try:
        current_sequence_value = request.args.get("current_stop_sequence", "")
        current_status_value = request.args.get("current_status", "")
        current_sequence = int(current_sequence_value) if current_sequence_value else None
        current_status = int(current_status_value) if current_status_value else None
        result = _trip_journey(
            trip_id,
            current_stop_sequence=current_sequence,
            current_status=current_status,
            current_stop_id=request.args.get("current_stop_id", "")[:200],
        )
        return jsonify(result)
    except KeyError as exc:
        return jsonify(error=str(exc).strip("'")), 404
    except (requests.RequestException, ValueError, zipfile.BadZipFile, sqlite3.Error) as exc:
        app.logger.warning("Impossible de charger le trajet %s : %s", trip_id, exc)
        return jsonify(error="Impossible de charger le parcours de ce véhicule."), 502


@app.get("/api/vehicles")
def vehicles():
    try:
        return jsonify(_fetch_vehicles())
    except requests.RequestException as exc:
        app.logger.warning("Impossible de lire le flux de positions TAM : %s", exc)
        return jsonify(error="Le flux de positions TAM est momentanément inaccessible."), 502
    except ValueError as exc:
        app.logger.warning("Flux de positions TAM invalide : %s", exc)
        return jsonify(error="Le flux reçu n'est pas lisible pour le moment."), 502


@app.get("/tc-infos/vehicle")
def tc_infos_vehicle():
    number = request.args.get("number", "").strip()
    mode = request.args.get("mode", "").strip().casefold()
    line = request.args.get("line", "").strip()[:32]
    if not number or len(number) > 40 or mode not in {"bus", "tramway"}:
        return "Numéro ou type de véhicule invalide.", 400

    profile_url = _resolve_tc_infos_vehicle(number, mode, line)
    return redirect(profile_url or _tc_infos_search_url(number), code=302)


if __name__ == "__main__":
    # Local access only: this is a small personal dashboard, not a public server.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
