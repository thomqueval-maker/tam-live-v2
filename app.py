"""Carte locale des bus et trams de Montpellier à partir des flux open data."""

from __future__ import annotations

import csv
import difflib
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
from flask import Flask, jsonify, render_template, request
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
VEHICLE_CACHE_SECONDS = 5
LINES_CACHE_SECONDS = 60 * 60
GTFS_CACHE_SECONDS = 6 * 60 * 60
TRIP_UPDATES_CACHE_SECONDS = 15
STOPS_CACHE_SECONDS = 6 * 60 * 60
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
            candidates.append((distance, -int(substring_match), -max(overlap, similarity), stop_id))
    candidates.sort()
    # Keep a few platforms at the same physical stop, but avoid spilling into a nearby station.
    return [item[3] for item in candidates[:8] if item[0] <= 130]


def _fetch_stops() -> list[dict]:
    """Load TAM stop points and keep only their names plus internal GTFS stop matches."""
    global _stops_cache
    now = time.monotonic()
    with _stops_lock:
        if _stops_cache and now - _stops_cache[0] < STOPS_CACHE_SECONDS:
            return _stops_cache[1]
        gtfs = _fetch_gtfs_data()
        stops = []
        errors = []
        for mode, url in (("tramway", TRAM_STOPS_URL), ("bus", BUS_STOPS_URL)):
            try:
                response = http.get(url, timeout=(5, 20))
                response.raise_for_status()
                geojson = response.json()
                if not isinstance(geojson, dict) or not isinstance(geojson.get("features"), list):
                    raise ValueError("Le fichier des arrêts n'est pas un GeoJSON valide.")
                for feature in geojson["features"]:
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
                    candidates = _gtfs_stop_candidates(name, latitude, longitude, gtfs["stops_by_id"])
                    stops.append({
                        "id": f"{mode}:{feature.get('id') or len(stops)}",
                        "name": name,
                        "mode": mode,
                        "latitude": latitude,
                        "longitude": longitude,
                        "gtfs_stop_ids": candidates,
                    })
            except (requests.RequestException, ValueError, TypeError) as exc:
                errors.append(f"{mode}: {exc}")
                app.logger.warning("Impossible de charger les arrêts %s : %s", mode, exc)
        if not stops and errors:
            raise ValueError("Impossible de charger les arrêts TAM. Vérifiez votre connexion.")
        _stops_cache = (time.monotonic(), stops)
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
                "direction": trip.get("headsign") or route.get("long_name") or "Direction non précisée",
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
                    if stop_id not in stop_id_set and connection is not None:
                        scheduled_row = _scheduled_stop_time(
                            connection, trip_id, stop_id,
                            int(stop_update.stop_sequence) if stop_update.HasField("stop_sequence") else None,
                        ) if trip_id else None
                        if scheduled_row:
                            stop_id = scheduled_row[0]
                    else:
                        scheduled_row = _scheduled_stop_time(connection, trip_id, stop_id, None) if connection and trip_id and stop_id else None
                    if stop_id not in stop_id_set:
                        continue
                    event = stop_update.arrival if stop_update.HasField("arrival") else (
                        stop_update.departure if stop_update.HasField("departure") else None
                    )
                    if event is None:
                        continue
                    event_time = datetime.fromtimestamp(event.time, PARIS_TZ) if event.HasField("time") else None
                    if event_time is None and event.HasField("delay") and scheduled_row:
                        scheduled = _gtfs_time_datetime(service_date, scheduled_row[2] or scheduled_row[3])
                        if scheduled:
                            event_time = scheduled + timedelta(seconds=int(event.delay))
                    if not event_time or event_time < now - timedelta(minutes=1) or event_time > now + timedelta(hours=8):
                        continue
                    vehicle = ""
                    if update.HasField("vehicle"):
                        vehicle = update.vehicle.label or update.vehicle.id
                    departures.append({
                        "timestamp": event_time.timestamp(),
                        "route": route.get("short_name") or route.get("long_name") or "?",
                        "color": route.get("color") or ("#0876b9" if mode == "tramway" else "#19785e"),
                        "direction": trip.get("headsign") or route.get("long_name") or "Direction non précisée",
                        "vehicle": vehicle,
                        "delay": int(event.delay) if event.HasField("delay") else None,
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
        "direction": trip_info.get("headsign", ""),
        "latitude": latitude,
        "longitude": longitude,
        "bearing": int(position.bearing) if position.HasField("bearing") else None,
        "timestamp": timestamp,
        "current_status": current_status,
        "stop_id": vehicle.stop_id if vehicle.HasField("stop_id") else "",
    }


def _fetch_vehicles() -> dict:
    """Download and decode one GTFS-Realtime VehiclePositions feed."""
    global _vehicle_cache
    now = time.monotonic()
    with _vehicle_lock:
        if _vehicle_cache and now - _vehicle_cache[0] < VEHICLE_CACHE_SECONDS:
            return _vehicle_cache[1]

        response = http.get(VEHICLE_POSITIONS_URL, timeout=12)
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
        return jsonify(error="Impossible de charger les arrêts. Vérifiez votre connexion."), 502


@app.get("/api/stop-schedules")
def stop_schedules():
    mode = request.args.get("mode", "")
    tab = request.args.get("tab", "realtime")
    if mode not in {"bus", "tramway"} or tab not in {"realtime", "theoretical"}:
        return jsonify(error="Paramètres d’horaires invalides."), 400
    requested_ids = [value for value in request.args.get("stop_ids", "").split(",") if value]
    if not requested_ids or len(requested_ids) > 12:
        return jsonify(departures=[], service_date=_service_date_now().isoformat(), updated_at=None)
    try:
        gtfs = _fetch_gtfs_data()
        stop_ids = [stop_id for stop_id in dict.fromkeys(requested_ids) if stop_id in gtfs["stops_by_id"]]
        result = _schedule_departures(stop_ids, mode, tab)
        return jsonify(result)
    except (requests.RequestException, ValueError, zipfile.BadZipFile, sqlite3.Error) as exc:
        app.logger.warning("Impossible de charger les horaires %s : %s", mode, exc)
        return jsonify(error="Impossible de charger les horaires pour cet arrêt."), 502


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


if __name__ == "__main__":
    # Local access only: this is a small personal dashboard, not a public server.
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "5000")), debug=False)
