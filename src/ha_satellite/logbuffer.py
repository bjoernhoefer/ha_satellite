"""In-Memory-Log für die Web-UI (``GET /api/logs``).

Ein ``logging.Handler`` hält die letzten Einträge in einem Ringpuffer. Jeder
Eintrag bekommt eine fortlaufende ID, damit die UI per ``?after=<id>`` nur
neue Zeilen nachladen kann. Zusätzlich wird optional in eine rotierende
Datei unter ``<Datenverzeichnis>/logs/`` geschrieben, damit Logs einen
Neustart überleben.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

# Zugriffslogs würden durch das Polling der UI alles andere verdrängen.
_IGNORED_LOGGERS = ("uvicorn.access",)


class LogBuffer(logging.Handler):
    def __init__(self, capacity: int = 1000) -> None:
        super().__init__(level=logging.INFO)
        self._entries: deque[dict] = deque(maxlen=capacity)
        self._next_id = 1
        self._entries_lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith(_IGNORED_LOGGERS)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info:
                message += "\n" + logging.Formatter().formatException(record.exc_info)
            with self._entries_lock:
                self._entries.append(
                    {
                        "id": self._next_id,
                        "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                        "level": record.levelname,
                        "logger": record.name,
                        "message": message,
                    }
                )
                self._next_id += 1
        except Exception:  # pragma: no cover - Logging darf nie crashen
            self.handleError(record)

    def entries(self, after: int = 0, limit: int = 500) -> list[dict]:
        with self._entries_lock:
            result = [entry for entry in self._entries if entry["id"] > after]
        return result[-limit:] if limit > 0 else result


def install(capacity: int = 1000, log_dir: Path | None = None) -> LogBuffer:
    """Hängt den Puffer (und ggf. eine Logdatei) an Root- und uvicorn-Logger."""
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        stream = logging.StreamHandler()
        stream.setFormatter(logging.Formatter(LOG_FORMAT))
        root.addHandler(stream)

    # APScheduler protokolliert jeden Job-Lauf auf INFO - das übertönt die
    # eigentlichen Render-Meldungen in der UI.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)

    buffer = LogBuffer(capacity)
    handlers: list[logging.Handler] = [buffer]
    if log_dir is not None:
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                log_dir / "ha_satellite.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(logging.INFO)
            file_handler.setFormatter(logging.Formatter(LOG_FORMAT))
            file_handler.addFilter(buffer.filter)
            handlers.append(file_handler)
        except OSError as exc:
            logging.getLogger(__name__).warning("Logdatei in %s nicht möglich: %s", log_dir, exc)

    for handler in handlers:
        root.addHandler(handler)
        # uvicorn.error propagiert nicht bis zum Root-Logger.
        logging.getLogger("uvicorn.error").addHandler(handler)
    return buffer
