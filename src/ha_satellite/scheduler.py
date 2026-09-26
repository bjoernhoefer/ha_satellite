"""Nicht-blockierender Scheduler für Download/Rendering.

Nutzt APScheduler mit einem eigenen Thread-Pool, sodass der FastAPI-
Webserver (asyncio) niemals auf Download oder Rendering wartet. Ein
globaler Lock stellt sicher, dass immer nur ein Render-Vorgang
gleichzeitig läuft ("Ein Render-Vorgang zur Zeit") - wichtig, da sich
der Pi 5 die Ressourcen mit Grafana teilt.

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
from ha_satellite.sources import RenderError, get_source
from ha_satellite.status import StatusStore

if TYPE_CHECKING:
    from ha_satellite.source_sync import SourceSync

logger = logging.getLogger(__name__)

JOB_ID_PREFIX = "render-"
# Versatz der Regionen untereinander: sonst starten alle Jobs gleichzeitig,
# und wegen des globalen Render-Locks würden alle bis auf einen übersprungen.
REGION_STAGGER_SECONDS = 60
SYNC_JOB_ID = "source-sync"


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
        for job in self._scheduler.get_jobs():
            if job.id.startswith(JOB_ID_PREFIX) or job.id == SYNC_JOB_ID:
                job.remove()
        config = self._config_store.get()
        interval = config.sources.poll_interval_minutes
        self._schedule_sync(config.sources.auto_sync_hours)
        now = datetime.now(timezone.utc)
        offset = 0
        for region in config.regions:
            if not config.sources.is_enabled(region.source):
                logger.info(
                    "Region %s: Quelle '%s' ist deaktiviert - keine automatischen Läufe",
                    region.name, region.source,
                )
                continue
            job_id = f"{JOB_ID_PREFIX}{region.name}"
            self._scheduler.add_job(
                self._run_region,
                "interval",
                minutes=interval,
                args=[region.name],
                id=job_id,
                next_run_time=now + timedelta(seconds=offset * REGION_STAGGER_SECONDS),
                max_instances=1,
                coalesce=True,
            )
            offset += 1
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

        if not self._render_lock.acquire(blocking=False):
            logger.info("Render-Vorgang bereits aktiv, überspringe %s in diesem Zyklus", region_name)
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
            png_bytes = source.render(region, config.eumetsat)
            buffer = self._buffers.get(
                region.name, config.max_frames_for(region), config.history.max_storage_mb
            )
            frame = buffer.add_frame(png_bytes)
            self._status.record_success(region.name, len(buffer), next_run_at)
            logger.info(
                "Render %s fertig in %.1f s: %s (%d KB), %d Frames im Puffer",
                region.name, time.monotonic() - started, frame.filename,
                len(png_bytes) // 1024, len(buffer),
            )
        except RenderError as exc:
            logger.error("Rendering für %s fehlgeschlagen: %s", region_name, exc)
            self._status.record_error(region.name, str(exc), next_run_at)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unerwarteter Fehler beim Rendern von %s", region_name)
            self._status.record_error(region.name, str(exc), next_run_at)
        finally:
            self._render_lock.release()
