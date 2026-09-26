"""Nicht-blockierender Scheduler für Download/Rendering.

Nutzt APScheduler mit einem eigenen Thread-Pool, sodass der FastAPI-
Webserver (asyncio) niemals auf Download oder Rendering wartet. Ein
globaler Lock stellt sicher, dass immer nur ein Render-Vorgang
gleichzeitig läuft ("Ein Render-Vorgang zur Zeit") - wichtig, da sich
der Pi 5 die Ressourcen mit Grafana teilt. Gleichzeitig fällige Regionen
warten aufeinander (statt übersprungen zu werden) und nutzen dabei
dasselbe, nur einmal heruntergeladene Produkt.

Fehler (fehlende Slots, API-Timeouts, ungültige Credentials) werden
geloggt und im StatusStore vermerkt, führen aber nicht zum Absturz;
der nächste planmäßige Lauf versucht es erneut (einfaches Retry).
"""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

from ha_satellite.buffer import BufferManager
from ha_satellite.config import AppConfig, ConfigStore
from ha_satellite.sources import NoNewData, RenderError, get_source, sync_fci_archive
from ha_satellite.status import StatusStore

if TYPE_CHECKING:
    from ha_satellite.source_sync import SourceSync

logger = logging.getLogger(__name__)

JOB_ID_PREFIX = "render-"
ARCHIVE_JOB_PREFIX = "archive-"
SYNC_JOB_ID = "source-sync"
# Download (~100 MB) + Rendering einer Region bleibt deutlich darunter.
LOCK_TIMEOUT_SECONDS = 15 * 60


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class RenderScheduler:
    def __init__(
        self,
        config_store: ConfigStore,
        buffer_manager: BufferManager,
        status_store: StatusStore,
        source_sync: "SourceSync | None" = None,
    ) -> None:
        self._config_store = config_store
        self._buffers = buffer_manager
        self._status = status_store
        self._source_sync = source_sync
        self._render_lock = threading.Lock()
        self._scheduler = BackgroundScheduler()
        # Region -> Einstellungen beim letzten Aufsetzen (erkennt Änderungen).
        self._signatures: dict[str, tuple[int, str]] = {}

    def start(self) -> None:
        self._reschedule()
        self._scheduler.start()

    def shutdown(self) -> None:
        self._scheduler.shutdown(wait=False)

    def reload(self) -> None:
        """Nach Konfigurationsänderungen: Jobs neu aufsetzen."""
        self._reschedule()

    @contextmanager
    def exclusive(self, timeout: float = 120.0):
        """Blockiert Render-Läufe (z. B. während des Speicherort-Wechsels)."""
        if not self._render_lock.acquire(timeout=timeout):
            raise TimeoutError("Render-Vorgang läuft noch, bitte später erneut versuchen")
        try:
            yield
        finally:
            self._render_lock.release()

    def _reschedule(self) -> None:
        previous_runs: dict[str, datetime | None] = {}
        for job in self._scheduler.get_jobs():
            if job.id.startswith((JOB_ID_PREFIX, ARCHIVE_JOB_PREFIX)) or job.id == SYNC_JOB_ID:
                previous_runs[job.id] = getattr(job, "next_run_time", None)
                job.remove()
        config = self._config_store.get()
        interval = config.sources.poll_interval_minutes
        self._schedule_sync(config.sources.auto_sync_hours)
        now = datetime.now(timezone.utc)
        # FCI-Rohdaten-Archiv: unabhängig davon, ob eine Region die Quelle
        # nutzt - so lassen sich später beliebige Regionen/Komposite rendern.
        for entry in config.sources.catalog:
            if entry.enabled and entry.driver == "mtg_fci":
                job_id = f"{ARCHIVE_JOB_PREFIX}{entry.id}"
                self._scheduler.add_job(
                    self._run_archive,
                    "interval",
                    minutes=interval,
                    args=[entry.id],
                    id=job_id,
                    next_run_time=previous_runs.get(job_id) or now,
                    max_instances=1,
                    coalesce=True,
                )
        for region in config.regions:
            if not config.sources.is_enabled(region.source):
                logger.info(
                    "Region %s: Quelle '%s' ist deaktiviert - keine automatischen Läufe",
                    region.name, region.source,
                )
                continue
            job_id = f"{JOB_ID_PREFIX}{region.name}"
            # Unveränderte Regionen behalten ihren Takt; neue oder geänderte
            # (z. B. andere Quelle/Komposit) werden sofort neu gerendert.
            signature = (interval, region.model_dump_json())
            next_run = previous_runs.get(job_id)
            if next_run is None or self._signatures.get(region.name) != signature:
                if region.name in self._signatures:
                    logger.info("Region %s geändert - rendere sofort neu", region.name)
                next_run = now
            self._signatures[region.name] = signature
            self._scheduler.add_job(
                self._run_region,
                "interval",
                minutes=interval,
                args=[region.name],
                id=job_id,
                next_run_time=next_run,
                max_instances=1,
                coalesce=True,
            )
        logger.info("Scheduler neu aufgesetzt: Abrufintervall %d min", interval)

    def _schedule_sync(self, hours: int) -> None:
        if self._source_sync is None or hours <= 0:
            return
        last = self._source_sync.last_run_at()
        now = datetime.now(timezone.utc)
        first = now + timedelta(seconds=30)
        if last is not None and last + timedelta(hours=hours) > first:
            first = last + timedelta(hours=hours)
        self._scheduler.add_job(
            self._source_sync.run,
            "interval",
            hours=hours,
            id=SYNC_JOB_ID,
            next_run_time=first,
            max_instances=1,
            coalesce=True,
        )

    def _run_archive(self, source_id: str) -> None:
        """Lädt den neuesten FCI-Slot ins Archiv (ohne Render-Lock: nur I/O)."""
        config = self._config_store.get()
        entry = config.sources.get(source_id)
        if entry is None or entry.driver != "mtg_fci":
            return
        try:
            slot = sync_fci_archive(config, entry.collection or "")
            logger.debug("FCI-Archiv %s aktuell: %s", source_id, slot.name)
        except RenderError as exc:
            logger.error("FCI-Archiv %s: %s", source_id, exc)
        except Exception:  # pragma: no cover - defensive
            logger.exception("FCI-Archiv %s: unerwarteter Fehler", source_id)

    def trigger_now(self, region_name: str) -> None:
        """Für den UI-Button "Jetzt aktualisieren"."""
        self._scheduler.add_job(
            self._run_region,
            args=[region_name],
            id=f"manual-{region_name}-{datetime.now(timezone.utc).timestamp()}",
            max_instances=1,
        )

    def _run_region(self, region_name: str) -> None:
        config = self._config_store.get()
        region = config.region(region_name)
        if region is None:
            logger.error("Region %s existiert nicht mehr, überspringe Lauf", region_name)
            return

        next_run_at = datetime.now(timezone.utc) + timedelta(
            minutes=config.sources.poll_interval_minutes
        )

        if not self._render_lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            logger.warning("Render-Lock nicht erhalten, überspringe %s in diesem Zyklus", region_name)
            return
        try:
            definition = config.sources.get(region.source)
            if definition is None:
                raise RenderError(f"Quelle '{region.source}' ist nicht im Katalog")
            started = time.monotonic()
            logger.info(
                "Render %s gestartet (Quelle %s, Treiber %s, Komposit %s)",
                region.name, definition.id, definition.driver, region.composite,
            )
            source = get_source(definition.driver)
            buffer = self._buffers.get(
                region.name, config.max_frames_for(region), config.history.max_storage_mb
            )
            latest = buffer.latest()
            # Nur vergleichen, wenn der neueste Frame aus derselben Quelle mit
            # demselben Komposit stammt - sonst (Quellen-/Kompositwechsel) sofort
            # neu rendern, auch wenn die Aufnahme älter ist (0° hinkt Rapid Scan
            # ~15 min hinterher).
            same_origin = (
                latest is not None
                and latest.source == region.source
                and latest.composite == region.composite
            )
            last_sensing = _parse_timestamp(latest.created_at) if same_origin else None
            try:
                rendered = source.render(region, config, last_sensing)
            except NoNewData as info:
                logger.info("Keine neue Aufnahme für %s: %s", region_name, info)
            else:
                frame = buffer.add_frame(
                    rendered.png,
                    timestamp=rendered.sensing_time,
                    source=region.source,
                    composite=region.composite,
                )
                logger.info(
                    "Render %s fertig in %.1f s: %s (%d KB), %d Frames im Puffer",
                    region.name, time.monotonic() - started, frame.filename,
                    len(rendered.png) // 1024, len(buffer),
                )
            self._status.record_success(region.name, len(buffer), next_run_at)
        except RenderError as exc:
            logger.error("Rendering für %s fehlgeschlagen: %s", region_name, exc)
            self._status.record_error(region.name, str(exc), next_run_at)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unerwarteter Fehler beim Rendern von %s", region_name)
            self._status.record_error(region.name, str(exc), next_run_at)
        finally:
            self._render_lock.release()
