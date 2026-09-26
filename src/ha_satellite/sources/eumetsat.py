"""Zugriff auf den EUMETSAT Data Store (Suche + Download via ``eumdac``).

Pro Collection wird nur das jeweils neueste Produkt vorgehalten. Da alle
Regionen dieselbe Szene nutzen (z. B. den Rapid Scan über Europa), wird
jedes Produkt genau einmal heruntergeladen und von allen Regionen
gerendert - bei ~100 MB pro Slot ist das der entscheidende Hebel für das
Datenvolumen.
"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ha_satellite.config import EumetsatCredentials

logger = logging.getLogger(__name__)

# Suchfenster für "neuestes Produkt". Der Rapid Scan liefert alle 5 Minuten,
# Full Disc alle 15 Minuten - 2 Stunden decken auch Lücken im Betrieb ab.
SEARCH_WINDOW = timedelta(hours=2)


class DataStoreError(Exception):
    """Fehler beim Zugriff auf den Data Store (Auth, Suche, Download)."""


@dataclass(frozen=True)
class Product:
    product_id: str
    sensing_end: datetime
    path: Path


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ProductCache:
    """Hält das neueste Produkt je Collection auf der Platte vor."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._lock = threading.Lock()

    def latest(
        self,
        collection_id: str,
        credentials: EumetsatCredentials,
        entry_suffix: str,
    ) -> Product:
        if not credentials.consumer_key or not credentials.consumer_secret:
            raise DataStoreError(
                "Keine EUMETSAT-Zugangsdaten hinterlegt (Web-UI oder Umgebungsvariablen)"
            )
        import eumdac  # teuer im Import, nur bei echtem Bedarf laden

        with self._lock:
            try:
                token = eumdac.AccessToken(
                    (credentials.consumer_key, credentials.consumer_secret)
                )
                collection = eumdac.DataStore(token).get_collection(collection_id)
                now = datetime.now(timezone.utc)
                results = collection.search(
                    dtstart=(now - SEARCH_WINDOW).replace(tzinfo=None),
                    dtend=now.replace(tzinfo=None),
                )
                newest = next(iter(results), None)
            except Exception as exc:
                raise DataStoreError(f"Data-Store-Suche fehlgeschlagen: {exc}") from exc

            if newest is None:
                raise DataStoreError(
                    f"Kein Produkt in {collection_id} innerhalb der letzten "
                    f"{int(SEARCH_WINDOW.total_seconds() // 3600)} Stunden"
                )

            product_id = str(newest)
            target_dir = self._cache_dir / _safe_name(collection_id)
            target = target_dir / f"{product_id}{entry_suffix}"
            product = Product(product_id, _as_utc(newest.sensing_end), target)
            if target.exists():
                return product

            entry = next((e for e in newest.entries if e.endswith(entry_suffix)), None)
            if entry is None:
                raise DataStoreError(
                    f"Produkt {product_id} enthält keine Datei '*{entry_suffix}'"
                )
            logger.info("Lade %s (%s) herunter", product_id, collection_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".part")
            try:
                with newest.open(entry=entry) as source, partial.open("wb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
                partial.rename(target)
            except Exception as exc:
                partial.unlink(missing_ok=True)
                raise DataStoreError(f"Download von {product_id} fehlgeschlagen: {exc}") from exc

            # Nur das neueste Produkt behalten.
            for old in target_dir.iterdir():
                if old != target:
                    old.unlink(missing_ok=True)
            return product


def _safe_name(collection_id: str) -> str:
    return collection_id.replace(":", "_")
