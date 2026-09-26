"""Abgleich des Quellen-Katalogs mit dem EUMETSAT Data Store.

Nutzt ausschließlich die öffentlichen (nicht authentifizierten) Browse- und
Search-Endpunkte der EUMETSAT Data Store API:

- ``/data/browse/1.0.0/collections`` - Liste aller Collections (ID, Titel,
  Anzahl Produkte). Daraus ergibt sich, ob eine im Katalog eingetragene
  Collection (noch) existiert, und welche weiteren, von einem vorhandenen
  Treiber unterstützten Collections neu verfügbar sind ("entdeckt").
- ``/data/search-products/1.0.0/os`` - liefert u. a. den Zeitstempel des
  jüngsten Produkts einer Collection (Aktualität der Quelle).

Das Ergebnis wird getrennt von der Konfiguration in
``<Datenverzeichnis>/source_sync.json`` abgelegt, damit der in der UI
editierbare Katalog nicht mit Laufzeit-Metadaten vermischt wird.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from ha_satellite.config import ConfigStore, SourceDefinition

logger = logging.getLogger(__name__)

API_BASE_ENV = "HA_SATELLITE_EUMETSAT_API"
DEFAULT_API_BASE = "https://api.eumetsat.int"
REQUEST_TIMEOUT = 20

# Welche Collections ein vorhandener Treiber verarbeiten kann (Titel-Muster).
DISCOVERY_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("msg_seviri", re.compile(r"^(Rapid Scan )?High Rate SEVIRI Level 1\.5 Image Data - MSG")),
    ("mtg_fci", re.compile(r"^FCI Level 1c .*Image Data - MTG")),
)


def api_base() -> str:
    return os.environ.get(API_BASE_ENV, DEFAULT_API_BASE).rstrip("/")


def _get_json(url: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def _latest_product_at(collection: str) -> str | None:
    query = urllib.parse.urlencode({"pi": collection, "format": "json", "si": 0, "c": 1})
    data = _get_json(f"{api_base()}/data/search-products/1.0.0/os?{query}")
    for param in data.get("properties", {}).get("parameters", []):
        if param.get("name") == "dtstart" and param.get("maxInclusive"):
            return param["maxInclusive"]
    for feature in data.get("features", []):
        date = feature.get("properties", {}).get("date")
        if isinstance(date, str):
            return date.split("/")[-1]
    return None


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


class SourceSync:
    def __init__(self, config_store: ConfigStore, state_path: Path) -> None:
        self._config_store = config_store
        self._state_path = Path(state_path)
        self._run_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"last_run_at": None, "error": None, "sources": {}, "discovered": []}

    def _save(self, state: dict) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except OSError as exc:
            logger.warning("Abgleich-Ergebnis konnte nicht gespeichert werden: %s", exc)

    def state(self) -> dict:
        with self._state_lock:
            return json.loads(json.dumps(self._state))

    def last_run_at(self) -> datetime | None:
        value = self.state().get("last_run_at")
        return datetime.fromisoformat(value) if value else None

    def run(self) -> dict:
        """Führt den Abgleich aus. Fehler landen im Ergebnis, nie als Exception."""
        if not self._run_lock.acquire(blocking=False):
            logger.info("Quellen-Abgleich läuft bereits")
            return self.state()
        try:
            return self._run()
        finally:
            self._run_lock.release()

    def _run(self) -> dict:
        now = datetime.now(timezone.utc).isoformat()
        catalog = self._config_store.get().sources.catalog
        base = api_base()
        logger.info("Quellen-Abgleich mit %s gestartet (%d Katalogeinträge)", base, len(catalog))
        previous = self.state()
        try:
            listing = _get_json(f"{base}/data/browse/1.0.0/collections?format=json")
        except Exception as exc:
            logger.error("Quellen-Abgleich fehlgeschlagen: %s", exc)
            state = {**previous, "last_run_at": now, "error": str(exc)}
            with self._state_lock:
                self._state = state
            self._save(state)
            return self.state()

        collections = {
            link.get("title"): link
            for link in listing.get("links", [])
            if isinstance(link, dict) and link.get("title")
        }
        sources: dict[str, dict] = {}
        for entry in catalog:
            sources[entry.id] = self._check_entry(entry, collections, now)

        known = {(entry.driver, entry.collection) for entry in catalog}
        discovered = []
        for collection_id, link in sorted(collections.items()):
            title = link.get("datasetTitle", "")
            for driver, pattern in DISCOVERY_RULES:
                if pattern.search(title) and (driver, collection_id) not in known:
                    discovered.append(
                        {"collection": collection_id, "driver": driver, "title": title}
                    )
        state = {"last_run_at": now, "error": None, "sources": sources, "discovered": discovered}
        with self._state_lock:
            self._state = state
        self._save(state)
        ok = sum(1 for s in sources.values() if s["status"] == "ok")
        logger.info(
            "Quellen-Abgleich fertig: %d/%d verfügbar, %d neu entdeckt",
            ok, len(sources), len(discovered),
        )
        return self.state()

    def _check_entry(self, entry: SourceDefinition, collections: dict, now: str) -> dict:
        result = {"checked_at": now, "title": None, "latest_product_at": None,
                  "status": "ok", "message": None}
        if not entry.collection:
            result.update(status="local", message="Keine Collection hinterlegt")
            return result
        link = collections.get(entry.collection)
        if link is None:
            result.update(status="missing", message="Collection im Data Store nicht gefunden")
            logger.warning("Quelle %s: Collection %s nicht (mehr) verfügbar", entry.id, entry.collection)
            return result
        result["title"] = link.get("datasetTitle")
        try:
            result["latest_product_at"] = _latest_product_at(entry.collection)
        except Exception as exc:
            result.update(status="error", message=f"Suche fehlgeschlagen: {exc}")
            logger.warning("Quelle %s: Abfrage des letzten Produkts fehlgeschlagen: %s", entry.id, exc)
        return result

    def forget_discovered(self, collection: str) -> None:
        with self._state_lock:
            self._state["discovered"] = [
                item for item in self._state.get("discovered", [])
                if item["collection"] != collection
            ]
            state = json.loads(json.dumps(self._state))
        self._save(state)

    def definition_for(self, collection: str, existing_ids: set[str]) -> SourceDefinition | None:
        """Katalogeintrag (deaktiviert) für eine entdeckte Collection bauen."""
        for item in self.state().get("discovered", []):
            if item["collection"] != collection:
                continue
            base_id = f"{item['driver']}_{slugify(collection.split(':')[-1])}"
            source_id, n = base_id, 2
            while source_id in existing_ids:
                source_id, n = f"{base_id}_{n}", n + 1
            return SourceDefinition(
                id=source_id,
                driver=item["driver"],
                label=item["title"],
                collection=collection,
                enabled=False,
            )
        return None
