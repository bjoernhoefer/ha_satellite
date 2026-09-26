"""Konfigurations-Handling: YAML-Persistenz in /data mit Env-Var-Override.

Die Konfiguration wird als YAML-Datei gespeichert (Standardpfad
``/data/config.yaml``, überschreibbar über die Umgebungsvariable
``HA_SATELLITE_CONFIG``). EUMETSAT-Zugangsdaten können zusätzlich per
Umgebungsvariable (``EUMETSAT_CONSUMER_KEY`` / ``EUMETSAT_CONSUMER_SECRET``)
gesetzt werden; diese haben Vorrang vor den in der Datei gespeicherten
Werten, werden aber nicht in die Datei zurückgeschrieben.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from ha_satellite.geometry import bounding_box

DEFAULT_CONFIG_PATH_ENV = "HA_SATELLITE_CONFIG"
DEFAULT_CONFIG_PATH = "/data/config.yaml"

ENV_CONSUMER_KEY = "EUMETSAT_CONSUMER_KEY"
ENV_CONSUMER_SECRET = "EUMETSAT_CONSUMER_SECRET"

# "dummy" ist zusätzlich zulässig (reine Testquelle, siehe sources/__init__.py).
VALID_SOURCES = ("msg_seviri", "data_tailor", "mtg_fci", "dummy")

# Standard-Frames im Ringpuffer: entspricht (per Vorgabe) 60 Minuten Historie.
DEFAULT_HISTORY_MINUTES = 60


def config_path() -> Path:
    return Path(os.environ.get(DEFAULT_CONFIG_PATH_ENV, DEFAULT_CONFIG_PATH))


class EumetsatCredentials(BaseModel):
    consumer_key: str = ""
    consumer_secret: str = ""

    def masked(self) -> "EumetsatCredentials":
        """Gibt eine Kopie mit maskiertem Secret zurück (für die UI/API)."""
        secret = self.consumer_secret
        if not secret:
            masked_secret = ""
        elif len(secret) <= 4:
            masked_secret = "*" * len(secret)
        else:
            masked_secret = "*" * (len(secret) - 4) + secret[-4:]
        return EumetsatCredentials(consumer_key=self.consumer_key, consumer_secret=masked_secret)


class RegionConfig(BaseModel):
    name: str
    lat: float
    lon: float
    radius_km: float = Field(gt=0)
    width: int = 800
    height: int = 800
    composite: str = "natural_color"
    source: str = "msg_seviri"
    max_frames: int | None = None

    @field_validator("source")
    @classmethod
    def _validate_source(cls, value: str) -> str:
        if value not in VALID_SOURCES:
            raise ValueError(
                f"Unbekannte Quelle '{value}'. Erlaubt: {', '.join(VALID_SOURCES)}"
            )
        return value

    def bounding_box(self):
        return bounding_box(self.lat, self.lon, self.radius_km)

    def effective_max_frames(self, poll_interval_minutes: int) -> int:
        if self.max_frames is not None:
            return self.max_frames
        interval = max(poll_interval_minutes, 1)
        return max(DEFAULT_HISTORY_MINUTES // interval, 1)


class SourcesConfig(BaseModel):
    active: list[str] = Field(default_factory=lambda: ["msg_seviri"])
    poll_interval_minutes: int = Field(default=15, gt=0)


class HistoryConfig(BaseModel):
    max_storage_mb: int = Field(default=500, gt=0)


class ServerConfig(BaseModel):
    mjpeg_fps: float = Field(default=2.0, gt=0)


class AppConfig(BaseModel):
    eumetsat: EumetsatCredentials = Field(default_factory=EumetsatCredentials)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    regions: list[RegionConfig] = Field(default_factory=list)

    def region(self, name: str) -> RegionConfig | None:
        for region in self.regions:
            if region.name == name:
                return region
        return None

    def masked(self) -> "AppConfig":
        data = self.model_dump()
        data["eumetsat"] = self.eumetsat.masked().model_dump()
        return AppConfig(**data)


def default_config() -> AppConfig:
    """Standardkonfiguration mit vorkonfigurierten Regionen Wien und Mallorca."""
    return AppConfig(
        regions=[
            RegionConfig(
                name="wien",
                lat=48.2082,
                lon=16.3738,
                radius_km=300,
                composite="natural_color",
                source="msg_seviri",
            ),
            RegionConfig(
                name="mallorca",
                lat=39.6953,
                lon=3.0176,
                radius_km=300,
                composite="natural_color",
                source="msg_seviri",
            ),
        ]
    )


def env_overrides() -> dict[str, str]:
    """Liefert die per Umgebung gesetzten Zugangsdaten.

    Leere Werte zählen nicht als Override: `docker compose` setzt bei
    ``${VAR:-}`` eine leere Variable, die sonst die in der UI gespeicherten
    Zugangsdaten überdecken würde.
    """
    overrides: dict[str, str] = {}
    for field, env_name in (
        ("consumer_key", ENV_CONSUMER_KEY),
        ("consumer_secret", ENV_CONSUMER_SECRET),
    ):
        value = os.environ.get(env_name, "").strip()
        if value:
            overrides[field] = value
    return overrides


def _apply_env_overrides(config: AppConfig) -> AppConfig:
    overrides = env_overrides()
    if not overrides:
        return config
    data = config.model_dump()
    data["eumetsat"].update(overrides)
    return AppConfig(**data)


def load_config(path: Path | None = None) -> AppConfig:
    """Lädt die Konfiguration aus der YAML-Datei (oder liefert die Vorgabe)."""
    resolved = path or config_path()
    if resolved.exists():
        raw: dict[str, Any] = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        config = AppConfig(**raw)
    else:
        config = default_config()
    return _apply_env_overrides(config)


def save_config(config: AppConfig, path: Path | None = None) -> None:
    """Speichert die Konfiguration als YAML-Datei (legt Verzeichnis ggf. an)."""
    resolved = path or config_path()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(
        yaml.safe_dump(config.model_dump(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )


class ConfigStore:
    """Thread-sicherer In-Memory-Zugriff auf die persistierte Konfiguration.

    Hält zwei Sichten: ``stored()`` ist exakt das, was in der YAML-Datei
    steht, ``get()`` ist die effektive Konfiguration inkl. Env-Override.
    Änderungen müssen immer auf ``stored()`` aufsetzen, sonst würden per
    Umgebung gesetzte Zugangsdaten in die Datei geschrieben.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or config_path()
        self._lock = threading.RLock()
        self._stored = self._load_stored()
        self._config = _apply_env_overrides(self._stored)

    def _load_stored(self) -> AppConfig:
        if self._path.exists():
            raw: dict[str, Any] = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
            return AppConfig(**raw)
        return default_config()

    @property
    def path(self) -> Path:
        return self._path

    def get(self) -> AppConfig:
        with self._lock:
            return self._config

    def stored(self) -> AppConfig:
        with self._lock:
            return self._stored

    def update(self, new_config: AppConfig) -> AppConfig:
        with self._lock:
            save_config(new_config, self._path)
            self._stored = new_config
            self._config = _apply_env_overrides(new_config)
            return self._config

    def reload(self) -> AppConfig:
        with self._lock:
            self._stored = self._load_stored()
            self._config = _apply_env_overrides(self._stored)
            return self._config
