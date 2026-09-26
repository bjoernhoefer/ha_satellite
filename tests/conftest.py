"""Gemeinsame Fixtures: echter uvicorn-Server mit isoliertem Datenverzeichnis.

Der Server läuft bewusst als eigener Prozess statt über ``TestClient``:
``ha_satellite.main`` liest Pfade und Konfiguration beim Import, und die
Klicktests brauchen ohnehin einen echten HTTP-Server für den Browser.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest


@dataclass
class LiveServer:
    url: str
    data_dir: Path

    @property
    def config_file(self) -> Path:
        return self.data_dir / "config.yaml"


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
        port = _free_port()
        server_env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("EUMETSAT_")
        }
        server_env.update(
            HA_SATELLITE_DATA_DIR=str(data_dir),
            HA_SATELLITE_CONFIG=str(data_dir / "config.yaml"),
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
        return LiveServer(url=url, data_dir=data_dir)

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
