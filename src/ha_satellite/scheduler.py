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
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from ha_satellite.buffer import BufferManager
from ha_satellite.config import AppConfig, ConfigStore
from ha_satellite.sources import RenderError, get_source
from ha_satellite.status import StatusStore

logger = logging.getLogger(__name__)

JOB_ID_PREFIX = "render-"


class RenderScheduler:
    def __init__(
        self,
        config_store: ConfigStore,
        buffer_manager: BufferManager,
        status_store: StatusStore,
    ) -> None:
        self._config_store = config_store
        self._buffers = buffer_manager
        self._status = status_store
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

    def _reschedule(self) -> None:
        for job in self._scheduler.get_jobs():
            job.remove()
        config = self._config_store.get()
        interval = config.sources.poll_interval_minutes
        for region in config.regions:
            if region.source not in config.sources.active:
                continue
            job_id = f"{JOB_ID_PREFIX}{region.name}"
            self._scheduler.add_job(
                self._run_region,
                "interval",
                minutes=interval,
                args=[region.name],
                id=job_id,
                next_run_time=datetime.now(timezone.utc),
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
            source = get_source(region.source)
            png_bytes = source.render(region, config.eumetsat)
            max_frames = region.effective_max_frames(config.sources.poll_interval_minutes)
            buffer = self._buffers.get(region.name, max_frames, config.history.max_storage_mb)
            buffer.add_frame(png_bytes)
            self._status.record_success(region.name, len(buffer), next_run_at)
        except RenderError as exc:
            logger.error("Rendering für %s fehlgeschlagen: %s", region_name, exc)
            self._status.record_error(region.name, str(exc), next_run_at)
        except Exception as exc:  # pragma: no cover - defensive
            logger.exception("Unerwarteter Fehler beim Rendern von %s", region_name)
            self._status.record_error(region.name, str(exc), next_run_at)
        finally:
            self._render_lock.release()
