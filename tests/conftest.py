"""Gemeinsame Fixtures: echter uvicorn-Server mit isoliertem Datenverzeichnis.

Der Server läuft bewusst als eigener Prozess statt über ``TestClient``:
``ha_satellite.main`` liest Pfade und Konfiguration beim Import, und die
Klicktests brauchen ohnehin einen echten HTTP-Server für den Browser.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest


@dataclass
class LiveServer:
    url: str
    data_dir: Path
    storage_root: Path

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.yaml"

    def frames(self, region: str) -> list[dict]:
        return httpx.get(f"{self.url}/api/regions/{region}/frames").json()["frames"]

    def ensure_frames(self, region: str, count: int, timeout: float = 20.0) -> list[dict]:
        """Stößt Render-Läufe an, bis mindestens ``count`` Frames da sind.

        Wiederholt den Trigger, da kollidierende Läufe (globaler Render-Lock)
        bewusst übersprungen werden.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frames = self.frames(region)
            if len(frames) >= count:
                return frames
            httpx.post(f"{self.url}/api/regions/{region}/refresh")
            time.sleep(0.3)
        raise AssertionError(f"{region}: weniger als {count} Frames nach {timeout}s")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_until_healthy(url: str, process: subprocess.Popen, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise RuntimeError(f"Server ist beim Start abgestürzt:\n{output}")
        try:
            if httpx.get(f"{url}/healthz", timeout=1.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.1)
    raise RuntimeError("Server wurde nicht rechtzeitig erreichbar")


@pytest.fixture
def start_server(tmp_path: Path) -> Iterator[Callable[..., LiveServer]]:
    """Fabrik für Server-Instanzen; ``env`` erlaubt z. B. EUMETSAT-Overrides."""
    processes: list[subprocess.Popen] = []

    def _start(env: dict[str, str] | None = None) -> LiveServer:
        data_dir = tmp_path / f"data{len(processes)}"
        data_dir.mkdir()
        storage_root = tmp_path / f"mnt{len(processes)}"
        storage_root.mkdir()
        port = _free_port()
        server_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("EUMETSAT_")
        }
        server_env.update(
            HA_SATELLITE_DATA_DIR=str(data_dir),
            HA_SATELLITE_CONFIG=str(data_dir / "config.yaml"),
            HA_SATELLITE_STORAGE_ROOTS=str(storage_root),
            # Nie gegen die echte EUMETSAT-API: geschlossener Port als Default.
            HA_SATELLITE_EUMETSAT_API="http://127.0.0.1:9",
        )
        server_env.update(env or {})
        process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "ha_satellite.main:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning",
            ],
            env=server_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(process)
        url = f"http://127.0.0.1:{port}"
        _wait_until_healthy(url, process)
        return LiveServer(url=url, data_dir=data_dir, storage_root=storage_root)

    yield _start

    for process in processes:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def live_server(start_server: Callable[..., LiveServer]) -> LiveServer:
    return start_server()


FAKE_COLLECTIONS = {
    "EO:EUM:DAT:MSG:HRSEVIRI": "High Rate SEVIRI Level 1.5 Image Data - MSG - 0 degree",
    "EO:EUM:DAT:MSG:MSG15-RSS": "Rapid Scan High Rate SEVIRI Level 1.5 Image Data - MSG",
    "EO:EUM:DAT:MSG:HRSEVIRI-IODC": "High Rate SEVIRI Level 1.5 Image Data - MSG - Indian Ocean",
    "EO:EUM:DAT:0662": "FCI Level 1c Normal Resolution Image Data - MTG - 0 degree",
    "EO:EUM:DAT:0665": "FCI Level 1c High Resolution Image Data - MTG - 0 degree",
    "EO:EUM:DAT:0678": "Cloud Mask (netCDF) - MTG - 0 degree",
}
FAKE_LATEST = "2026-09-26T10:30:10.395Z"


class _FakeEumetsat(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D401 - still
        pass

    def _json(self, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/data/browse/1.0.0/collections":
            self._json({"links": [
                {"title": cid, "datasetTitle": title, "numberOfProducts": 1}
                for cid, title in FAKE_COLLECTIONS.items()
            ]})
        elif parsed.path == "/data/search-products/1.0.0/os":
            self._json({"properties": {"parameters": [
                {"name": "dtstart", "maxInclusive": FAKE_LATEST}
            ]}, "features": []})
        else:
            self.send_error(404)


@pytest.fixture
def fake_eumetsat() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeEumetsat)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
