"""Build the TC Infos fleet-number index from a computer that can reach TC Infos."""

from __future__ import annotations

from datetime import datetime, timezone
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import urlencode, urljoin, urlparse
from urllib.error import URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_FILE = ROOT / "data" / "tc_infos_vehicles.json"
ROSTERS = {
    "bus": "https://tc-infos.fr/reseau/230/parc-bus-cars",
    "tramway": "https://tc-infos.fr/reseau/230/parc-trams-metros",
}
LIVE_VEHICLES_URL = os.getenv(
    "TAM_LIVE_API_URL",
    "https://tam-live-v2.onrender.com/api/vehicles",
)
PROFILE_PATH = re.compile(r"(?:^|/)vehicule/(\d+)/?$", re.IGNORECASE)


class FleetRosterParser(HTMLParser):
    """Collect vehicle-number links from a TC Infos fleet table."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[dict] = []
        self.links: list[str] = []
        self.row: dict | None = None
        self.cell: dict | None = None
        self.anchor: dict | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        attributes = dict(attrs)
        if tag == "tr":
            self.row = {"cells": [], "anchors": []}
            self.cell = None
            self.anchor = None
        if tag == "a":
            self.anchor = {"href": attributes.get("href", ""), "text": []}
            self.links.append(self.anchor["href"])
            if self.row is not None:
                self.row["anchors"].append(self.anchor)
            if self.cell is not None:
                self.cell["anchors"].append(self.anchor)
            return
        if self.row is None:
            return
        if tag in {"td", "th"}:
            self.cell = {"text": [], "anchors": []}
            self.row["cells"].append(self.cell)

    def handle_data(self, data: str) -> None:
        if self.row is None:
            return
        if self.cell is not None:
            self.cell["text"].append(data)
        if self.anchor is not None:
            self.anchor["text"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self.anchor = None
        elif tag in {"td", "th"}:
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
            self.cell = None
            self.anchor = None


def normalize_number(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


def parse_roster(html: str) -> tuple[dict[str, str], list[str], list[str]]:
    parser = FleetRosterParser()
    parser.feed(html)
    candidates: dict[str, set[str]] = {}
    for row in parser.rows:
        for anchor in row["anchors"]:
            match = PROFILE_PATH.search(re.split(r"[?#]", anchor["href"], maxsplit=1)[0])
            number = normalize_number(" ".join(anchor["text"]))
            if match and number:
                candidates.setdefault(number, set()).add(match.group(1))

    result: dict[str, str] = {}
    ambiguous: list[str] = []
    for number, profile_ids in candidates.items():
        if len(profile_ids) == 1:
            result[number] = next(iter(profile_ids))
        else:
            ambiguous.append(number)
    return result, ambiguous, parser.links


def search_url(number: str) -> str:
    query = urlencode({
        "mode": "id", "numero": number, "immatriculation": "", "vin": "",
        "constructeur": "", "modele": "", "sort": "numero",
    })
    return f"https://tc-infos.fr/recherche?{query}"


def resolve_live_number(number: str, mode: str, line: str) -> str | None:
    request = Request(search_url(number), headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
    })
    with urlopen(request, timeout=30) as response:
        html = response.read().decode(response.headers.get_content_charset() or "utf-8")
    parser = FleetRosterParser()
    parser.feed(html)
    wanted = normalize_number(number)
    normalized_line = normalize_number(line)
    line_token_match = re.search(r"\d+", normalized_line) if mode == "tramway" else None
    line_token = line_token_match.group(0) if line_token_match else normalized_line
    candidates: list[tuple[int, str]] = []
    for row in parser.rows:
        cells = row["cells"]
        if len(cells) < 7:
            continue
        cell_text = [" ".join(cell["text"]) for cell in cells]
        assignment = normalize_number(cell_text[6])
        if "tam" not in assignment or "montpellier" not in assignment:
            continue
        model = normalize_number(cell_text[3]).split()
        is_tram = any(word in model for word in ("citadis", "tfs", "urbos", "tramway"))
        if (mode == "tramway") != is_tram:
            continue

        for anchor in row["anchors"]:
            match = PROFILE_PATH.search(re.split(r"[?#]", anchor["href"], maxsplit=1)[0])
            aliases = [normalize_number(part) for part in re.split(r"[/|,;]", " ".join(anchor["text"]))]
            if not match or wanted not in aliases:
                continue
            score = 4 if "en service" in normalize_number(cell_text[-1]) else 0
            if line_token:
                row_lines = [normalize_number(" ".join(item["text"])) for cell in cells for item in cell["anchors"]]
                if line_token in row_lines:
                    score += 8
            candidates.append((score, match.group(1)))
            break

    if not candidates:
        return None
    best_score = max(score for score, _profile_id in candidates)
    best_ids = {profile_id for score, profile_id in candidates if score == best_score}
    return next(iter(best_ids)) if len(best_ids) == 1 else None


def main() -> int:
    vehicles: dict[str, dict[str, str]] = {}
    for mode, roster_url in ROSTERS.items():
        queue = [roster_url]
        visited: set[str] = set()
        mapping: dict[str, str] = {}
        ambiguous: set[str] = set()
        roster_path = urlparse(roster_url).path.rstrip("/")
        while queue:
            url = queue.pop(0)
            if url in visited:
                continue
            visited.add(url)
            request = Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0 Safari/537.36",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.7",
            })
            with urlopen(request, timeout=30) as response:
                html = response.read().decode(response.headers.get_content_charset() or "utf-8")
            page_mapping, page_ambiguous, links = parse_roster(html)
            ambiguous.update(page_ambiguous)
            for number, profile_id in page_mapping.items():
                if number in mapping and mapping[number] != profile_id:
                    ambiguous.add(number)
                else:
                    mapping[number] = profile_id
            for href in links:
                page_url = urljoin(url, href)
                page_path = urlparse(page_url).path
                page_match = re.fullmatch(re.escape(roster_path) + r"/page(\d+)", page_path)
                if page_match and int(page_match.group(1)) > 1 and page_url not in visited:
                    queue.append(page_url)

        for number in ambiguous:
            mapping.pop(number, None)
        # Protect the existing index if TC Infos returns a block page or an incomplete response.
        minimum = 150 if mode == "bus" else 50
        if len(mapping) < minimum:
            raise RuntimeError(
                f"La page {mode} n’a fourni que {len(mapping)} fiches exploitables; "
                "l’index existant est conservé. Vérifie la connexion puis réessaie."
            )
        vehicles[mode] = mapping
        print(f"{mode}: {len(mapping)} fiches ajoutées.")
        if ambiguous:
            print(f"{mode}: {len(ambiguous)} numéro(s) ambigu(s) ignoré(s).")

    try:
        request = Request(LIVE_VEHICLES_URL, headers={"User-Agent": "TAM-Tram-Live/1.0"})
        with urlopen(request, timeout=30) as response:
            live_data = json.loads(response.read().decode(response.headers.get_content_charset() or "utf-8"))
        alias_count = 0
        for vehicle in live_data.get("vehicles", []):
            mode = vehicle.get("route_mode")
            number = str(vehicle.get("label") or vehicle.get("id") or "").strip()
            if mode not in vehicles or not number:
                continue
            normalized_number = normalize_number(number)
            if normalized_number in vehicles[mode]:
                continue
            profile_id = resolve_live_number(number, mode, str(vehicle.get("route_short_name") or ""))
            if profile_id:
                vehicles[mode][normalized_number] = profile_id
                alias_count += 1
        print(f"Alias vérifiés pour les véhicules actuellement en service : {alias_count} ajouté(s).")
    except (URLError, OSError, ValueError) as exc:
        print(f"Flux des véhicules en direct indisponible; index du parc conservé : {exc}")

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": ROSTERS,
        "vehicles": vehicles,
    }
    temp_file = OUTPUT_FILE.with_suffix(".tmp")
    temp_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp_file, OUTPUT_FILE)
    print(f"Index enregistré : {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (URLError, RuntimeError, OSError) as exc:
        print(f"Échec de mise à jour de l’index TC Infos : {exc}", file=sys.stderr)
        raise SystemExit(1)
