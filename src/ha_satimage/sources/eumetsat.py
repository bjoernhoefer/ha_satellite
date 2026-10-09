"""Access to the EUMETSAT Data Store (search + download via ``eumdac``).

Only the newest product is kept per collection. Since all regions use the
same scene (e.g. the Rapid Scan over Europe), each product is downloaded
exactly once and rendered for all regions - at ~100 MB per slot this is
the key lever for data volume.
"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ha_satimage.config import EumetsatCredentials

logger = logging.getLogger(__name__)

# Search window for "newest product". Rapid Scan delivers every 5 minutes,
# Full Disc every 15 minutes - 2 hours also cover operational gaps.
SEARCH_WINDOW = timedelta(hours=2)


class DataStoreError(Exception):
    """Error accessing the Data Store (auth, search, download)."""


@dataclass(frozen=True)
class Product:
    product_id: str
    sensing_end: datetime
    path: Path


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


class ProductCache:
    """Keeps the newest product per collection on disk."""

    def __init__(self, cache_dir: Path) -> None:
        self._cache_dir = Path(cache_dir)
        self._lock = threading.Lock()
        # Most recently provided product per collection (rendering without network).
        self._current: dict[str, Product] = {}

    def cached(self, collection_id: str) -> Product | None:
        """Most recently downloaded product, without querying the Data Store."""
        product = self._current.get(collection_id)
        return product if product is not None and product.path.exists() else None

    def latest(
        self,
        collection_id: str,
        credentials: EumetsatCredentials,
        entry_suffix: str,
    ) -> Product:
        if not credentials.consumer_key or not credentials.consumer_secret:
            raise DataStoreError(
                "No EUMETSAT credentials configured (web UI or environment variables)"
            )
        import eumdac  # expensive import, only load when actually needed

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
                raise DataStoreError(f"Data Store search failed: {exc}") from exc

            if newest is None:
                raise DataStoreError(
                    f"No product in {collection_id} within the last "
                    f"{int(SEARCH_WINDOW.total_seconds() // 3600)} hours"
                )

            product_id = str(newest)
            target_dir = self._cache_dir / _safe_name(collection_id)
            target = target_dir / f"{product_id}{entry_suffix}"
            product = Product(product_id, _as_utc(newest.sensing_end), target)
            if target.exists():
                self._current[collection_id] = product
                return product

            entry = next((e for e in newest.entries if e.endswith(entry_suffix)), None)
            if entry is None:
                raise DataStoreError(
                    f"Product {product_id} contains no file '*{entry_suffix}'"
                )
            logger.info("Downloading %s (%s)", product_id, collection_id)
            target_dir.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(target.suffix + ".part")
            try:
                with newest.open(entry=entry) as source, partial.open("wb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
                partial.rename(target)
            except Exception as exc:
                partial.unlink(missing_ok=True)
                raise DataStoreError(f"Download of {product_id} failed: {exc}") from exc

            # Keep only the newest and the previous product: a render that is
            # currently running may still be reading the previous one.
            previous = self._current.get(collection_id)
            keep = {target, previous.path if previous else None}
            for old in target_dir.iterdir():
                if old not in keep:
                    old.unlink(missing_ok=True)
            self._current[collection_id] = product
            return product


def _safe_name(collection_id: str) -> str:
    return collection_id.replace(":", "_")
