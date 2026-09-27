"""Nicht-blockierender Scheduler: Download im Aufnahmetakt, Rendern danach.

Nutzt APScheduler mit einem eigenen Thread-Pool, sodass der FastAPI-
Webserver (asyncio) niemals auf Download oder Rendering wartet.

- **Download** je aktiver Quelle mit Download-Treiber (``msg_seviri``,
  ``mtg_fci``) im Takt der Quelle (Rapid Scan 5 min, FCI 10 min, 0° 15 min;
  ``SourcesConfig.cycle_for``). Der Zeitpunkt richtet sich nach der
  letzten Aufnahme: Aufnahmeende + Takt + beobachtete Lieferverzögerung.
  Ist das Produkt dann noch nicht da, wird jede Minute erneut gefragt.
  Downloads halten den Render-Lock nicht (nur I/O).
- **Rendern** wird durch eine neue Aufnahme ausgelöst (für alle Regionen
  der Quelle) und nutzt das bereits lokal liegende Produkt. Ein globaler
  Lock stellt sicher, dass immer nur ein Render-Vorgang gleichzeitig läuft
  ("Ein Render-Vorgang zur Zeit") - wichtig, da sich der Pi 5 die
  Ressourcen mit Grafana teilt. Quellen ohne Download (Platzhalter)
  rendern weiter im festen Intervall.
- Nach jedem neuen Frame werden Vorschaubild, JPEG und Animationen
  vorab erzeugt (``media.prewarm``), damit das Abspielen sofort startet.

Fehler (fehlende Slots, API-Timeouts, ungültige Credentials) werden
geloggt und im StatusStore vermerkt, führen aber nicht zum Absturz;
der nächste planmäßige Lauf versucht es erneut (einfaches Retry).
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from apscheduler.schedulers.background import BackgroundScheduler

from ha_satellite import media
from ha_satellite.buffer import BufferManager
from ha_satellite.config import DOWNLOAD_DRIVERS, AppConfig, ConfigStore
from ha_satellite.sources import SOURCE_REGISTRY, NoNewData, RenderError, get_source
from ha_satellite.status import StatusStore

if TYPE_CHECKING:
    from ha_satellite.source_sync import SourceSync

logger = logging.getLogger(__name__)

JOB_ID_PREFIX = "render-"
DOWNLOAD_JOB_PREFIX = "download-"
MEDIA_JOB_PREFIX = "media-"
SYNC_JOB_ID = "source-sync"
# Download (~100 MB) + Rendering einer Region bleibt deutlich darunter.
LOCK_TIMEOUT_SECONDS = 15 * 60
# Download-Jobs prüfen in diesem Abstand, ob sie fällig sind (ohne Netz).
TICK_SECONDS = 20
# Erwartetes Produkt noch nicht da: so oft erneut fragen.
RETRY_INTERVAL = timedelta(minutes=1)
# Nach einem Fehler (Auth, Timeout, ...) spätestens so erneut versuchen.
ERROR_RETRY = timedelta(minutes=5)
MIN_GAP = timedelta(seconds=15)


def _parse_timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@dataclass
class DownloadState:
    """Takt-Zustand einer Quelle (nur im Speicher)."""

    due: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=timezone.utc))
    last_sensing: datetime | None = None
    last_check_at: datetime | None = None
    last_new_at: datetime | None = None
    next_expected: datetime | None = None
    last_error: str | None = None
    # Verzögerung zwischen Aufnahmeende und Verfügbarkeit (letzte Produkte);
    # das Minimum nähert sich der echten Lieferverzögerung an.
    latencies: deque = field(default_factory=lambda: deque(maxlen=12))

    def latency(self) -> timedelta:
        return min(self.latencies) if self.latencies else timedelta(0)


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
        self._downloads: dict[str, DownloadState] = {}
        self._download_lock = threading.Lock()
        self._job_counter = itertools.count()
        self._initialized = False

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

    # -- Aufsetzen -----------------------------------------------------------
    @staticmethod
    def _downloads_for(driver: str) -> bool:
        cls = SOURCE_REGISTRY.get(driver)
        return driver in DOWNLOAD_DRIVERS and cls is not None and cls.downloads

    def download_sources(self, config: AppConfig) -> list[str]:
        """Quellen mit eigenem Download-Job: aktiv und genutzt (bzw. Archiv)."""
        used = {region.source for region in config.regions}
        return [
            entry.id
            for entry in config.sources.catalog
            if entry.enabled
            and self._downloads_for(entry.driver)
            and (entry.id in used or SOURCE_REGISTRY[entry.driver].archive_always)
        ]

    def _reschedule(self) -> None:
        previous_runs: dict[str, datetime | None] = {}
        for job in self._scheduler.get_jobs():
            if job.id.startswith((JOB_ID_PREFIX, DOWNLOAD_JOB_PREFIX)) or job.id == SYNC_JOB_ID:
                if job.id.startswith(f"{JOB_ID_PREFIX}now-"):
                    continue  # ausstehende Einmal-Renders bleiben
                previous_runs[job.id] = getattr(job, "next_run_time", None)
                job.remove()
        config = self._config_store.get()
        self._schedule_sync(config.sources.auto_sync_hours)
        now = datetime.now(timezone.utc)

        download_ids = self.download_sources(config)
        for source_id in download_ids:
            self._downloads.setdefault(source_id, DownloadState())
            self._scheduler.add_job(
                self._tick_download,
                "interval",
                seconds=TICK_SECONDS,
                args=[source_id],
                id=f"{DOWNLOAD_JOB_PREFIX}{source_id}",
                next_run_time=now,
                max_instances=1,
                coalesce=True,
            )
        cycles = ", ".join(
            f"{sid} {config.sources.cycle_for(sid)} min" for sid in download_ids
        ) or "keine"

        for region in config.regions:
            if not config.sources.is_enabled(region.source):
                logger.info(
                    "Region %s: Quelle '%s' ist deaktiviert - keine automatischen Läufe",
                    region.name, region.source,
                )
                self._signatures.pop(region.name, None)
                continue
            interval = config.sources.cycle_for(region.source)
            signature = (interval, region.model_dump_json())
            changed = self._signatures.get(region.name) != signature
            known = region.name in self._signatures
            self._signatures[region.name] = signature
            if region.source in download_ids:
                # Gerendert wird, sobald der Download eine neue Aufnahme hat.
                # Geänderte/neue Regionen sofort (beim Start übernimmt das
                # der erste Download).
                if changed and self._initialized:
                    if known:
                        logger.info("Region %s geändert - rendere sofort neu", region.name)
                    self._queue_render(region.name)
                continue
            # Quellen ohne Download (Platzhalter): festes Intervall wie bisher.
            job_id = f"{JOB_ID_PREFIX}{region.name}"
            next_run = previous_runs.get(job_id)
            if next_run is None or changed:
                if known and changed:
                    logger.info("Region %s geändert - rendere sofort neu", region.name)
                next_run = now
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
        self._initialized = True
        logger.info(
            "Scheduler neu aufgesetzt: Download-Takt %s; Abrufintervall sonst %d min",
            cycles, config.sources.poll_interval_minutes,
        )

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

    # -- Download --------------------------------------------------------------
    def _tick_download(self, source_id: str) -> None:
        state = self._downloads.setdefault(source_id, DownloadState())
        if datetime.now(timezone.utc) < state.due:
            return
        self.download(source_id)

    def download(self, source_id: str) -> bool:
        """Neueste Aufnahme der Quelle laden; bei neuer Aufnahme Regionen rendern.

        Liefert ``True``, wenn eine neue Aufnahme vorliegt.
        """
        config = self._config_store.get()
        entry = config.sources.get(source_id)
        if entry is None or not entry.enabled or not self._downloads_for(entry.driver):
            return False
        with self._download_lock:
            state = self._downloads.setdefault(source_id, DownloadState())
        cycle = timedelta(minutes=config.sources.cycle_for(source_id))
        checked = datetime.now(timezone.utc)
        state.last_check_at = checked
        try:
            sensing = get_source(entry.driver).fetch(entry, config)
        except Exception as exc:
            if isinstance(exc, RenderError):
                logger.error("Download %s fehlgeschlagen: %s", source_id, exc)
            else:  # pragma: no cover - defensive
                logger.exception("Download %s: unerwarteter Fehler", source_id)
            state.last_error = str(exc)
            state.due = checked + min(cycle, ERROR_RETRY)
            return False
        if sensing.tzinfo is None:
            sensing = sensing.replace(tzinfo=timezone.utc)
        state.last_error = None
        new = state.last_sensing is None or sensing > state.last_sensing
        if new:
            state.latencies.append(max(checked - sensing, timedelta(0)))
            state.last_sensing = sensing
            state.last_new_at = checked
        expected = state.last_sensing + cycle + state.latency()
        now = datetime.now(timezone.utc)
        if expected > now:
            due = expected
        elif now - expected < cycle:
            due = now + RETRY_INTERVAL
        else:
            due = now + cycle  # Datenstrom stockt: nicht jede Minute fragen
        state.due = max(due, now + MIN_GAP)
        state.next_expected = expected
        if new:
            regions = [
                r.name for r in config.regions
                if r.source == source_id and config.sources.is_enabled(r.source)
            ]
            logger.info(
                "Download %s: neue Aufnahme %s (Takt %d min, Verzögerung %d s), "
                "nächste erwartet %s, rendere %s",
                source_id, sensing.strftime("%H:%M"), cycle.total_seconds() // 60,
                state.latency().total_seconds(), expected.strftime("%H:%M:%S"),
                ", ".join(regions) or "keine Region",
            )
            for name in regions:
                self._queue_render(name)
        else:
            logger.debug("Download %s: noch keine neue Aufnahme", source_id)
        return new

    def download_status(self) -> dict[str, dict]:
        config = self._config_store.get()
        active = set(self.download_sources(config))
        result = {}
        for entry in config.sources.catalog:
            state = self._downloads.get(entry.id)
            result[entry.id] = {
                "cycle_minutes": config.sources.cycle_for(entry.id),
                "cycle_auto": entry.cycle_minutes is None,
                "downloads": self._downloads_for(entry.driver),
                "active": entry.id in active,
                "last_sensing": _iso(state.last_sensing) if state else None,
                "last_check_at": _iso(state.last_check_at) if state else None,
                "next_check_at": _iso(state.due) if state and entry.id in active
                and state.last_check_at else None,
                "next_expected": _iso(state.next_expected) if state else None,
                "latency_seconds": int(state.latency().total_seconds())
                if state and state.latencies else None,
                "last_error": state.last_error if state else None,
            }
        return result

    # -- Rendern ---------------------------------------------------------------
    def _queue_render(self, region_name: str) -> None:
        self._scheduler.add_job(
            self._run_region,
            args=[region_name],
            id=f"{JOB_ID_PREFIX}now-{region_name}-{next(self._job_counter)}",
            misfire_grace_time=None,
        )

    def trigger_now(self, region_name: str) -> None:
        """Für den UI-Button "Jetzt aktualisieren"."""
        self._scheduler.add_job(
            self._run_region,
            args=[region_name],
            id=f"manual-{region_name}-{next(self._job_counter)}",
            max_instances=1,
        )

    def _next_run_for(self, config: AppConfig, source_id: str) -> datetime:
        state = self._downloads.get(source_id)
        if state is not None and state.next_expected is not None:
            return max(state.next_expected, datetime.now(timezone.utc))
        return datetime.now(timezone.utc) + timedelta(minutes=config.sources.cycle_for(source_id))

    def _run_region(self, region_name: str) -> None:
        config = self._config_store.get()
        region = config.region(region_name)
        if region is None:
            logger.error("Region %s existiert nicht mehr, überspringe Lauf", region_name)
            return

        next_run_at = self._next_run_for(config, region.source)
        new_frame = None

        if not self._render_lock.acquire(timeout=LOCK_TIMEOUT_SECONDS):
            logger.warning("Render-Lock nicht erhalten, überspringe %s in diesem Zyklus", region_name)
            return
        try:
            definition = config.sources.get(region.source)
            if definition is None:
                raise RenderError(f"Quelle '{region.source}' ist nicht im Katalog")
            started = time.monotonic()
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
                and bool(latest.borders) == region.borders
            )
            last_sensing = _parse_timestamp(latest.created_at) if same_origin else None
            logger.info(
                "Render %s gestartet (Quelle %s, Treiber %s, Komposit %s)",
                region.name, definition.id, definition.driver, region.composite,
            )
            try:
                rendered = source.render(region, config, last_sensing)
            except NoNewData as info:
                logger.info("Keine neue Aufnahme für %s: %s", region_name, info)
            else:
                new_frame = buffer.add_frame(
                    rendered.png,
                    timestamp=rendered.sensing_time,
                    source=region.source,
                    composite=region.composite,
                    borders=region.borders,
                )
                logger.info(
                    "Render %s fertig in %.1f s: %s (%d KB), %d Frames im Puffer",
                    region.name, time.monotonic() - started, new_frame.filename,
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

        if new_frame is not None:
            # Eigener Job je Region (ausstehende werden zusammengefasst):
            # blockiert weder den Render-Lock noch den nächsten Lauf.
            self._scheduler.add_job(
                self._prewarm,
                args=[region_name, buffer, config.server.mjpeg_fps],
                id=f"{MEDIA_JOB_PREFIX}{region_name}",
                replace_existing=True,
                misfire_grace_time=None,
            )

    def _prewarm(self, region_name: str, buffer, fps: float) -> None:
        try:
            media.prewarm(buffer, fps)
        except Exception:  # pragma: no cover - defensive
            logger.exception("Vorschau/Animation für %s konnte nicht erzeugt werden", region_name)
