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
from pydantic import BaseModel, Field, field_validator, model_validator

from ha_satellite.geometry import bounding_box

DEFAULT_CONFIG_PATH_ENV = "HA_SATELLITE_CONFIG"
DEFAULT_CONFIG_PATH = "/data/config.yaml"

ENV_CONSUMER_KEY = "EUMETSAT_CONSUMER_KEY"
ENV_CONSUMER_SECRET = "EUMETSAT_CONSUMER_SECRET"

# Implementierungen ("Treiber") in sources/__init__.py. Welche konkreten
# Quellen zur Verfügung stehen, legt der Quellen-Katalog (sources.catalog)
# fest - mehrere Katalogeinträge dürfen denselben Treiber nutzen (z. B.
# msg_seviri für 0°, Rapid Scan und IODC). "dummy" ist eine reine Testquelle.
VALID_DRIVERS = ("msg_seviri", "data_tailor", "mtg_fci", "dummy")
# Rückwärtskompatibler Alias.
VALID_SOURCES = VALID_DRIVERS

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
    # Verweist auf die ``id`` eines Eintrags im Quellen-Katalog.
    source: str = "msg_seviri"
    max_frames: int | None = None

    def bounding_box(self):
        return bounding_box(self.lat, self.lon, self.radius_km)

    def effective_max_frames(
        self, poll_interval_minutes: int, history_minutes: int = DEFAULT_HISTORY_MINUTES
    ) -> int:
        if self.max_frames is not None:
            return self.max_frames
        interval = max(poll_interval_minutes, 1)
        return max(history_minutes // interval, 1)


class SourceDefinition(BaseModel):
    """Eintrag im Quellen-Katalog (in der UI als JSON editierbar)."""

    id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    driver: str
    label: str = ""
    collection: str | None = None
    enabled: bool = True
    description: str = ""

    @field_validator("driver")
    @classmethod
    def _validate_driver(cls, value: str) -> str:
        if value not in VALID_DRIVERS:
            raise ValueError(
                f"Unbekannter Treiber '{value}'. Erlaubt: {', '.join(VALID_DRIVERS)}"
            )
        return value


def default_catalog() -> list[SourceDefinition]:
    return [
        SourceDefinition(
            id="msg_seviri",
            driver="msg_seviri",
            label="MSG SEVIRI 0° (Full Disk, 15 min)",
            collection="EO:EUM:DAT:MSG:HRSEVIRI",
            enabled=True,
        ),
        SourceDefinition(
            id="msg_seviri_rss",
            driver="msg_seviri",
            label="MSG SEVIRI Rapid Scan (Europa, 5 min)",
            collection="EO:EUM:DAT:MSG:MSG15-RSS",
            enabled=False,
        ),
        SourceDefinition(
            id="data_tailor",
            driver="data_tailor",
            label="MSG SEVIRI via Data Tailor (serverseitiger Zuschnitt)",
            collection="EO:EUM:DAT:MSG:HRSEVIRI",
            enabled=False,
        ),
        SourceDefinition(
            id="mtg_fci",
            driver="mtg_fci",
            label="MTG FCI Level 1c Normal Resolution (10 min)",
            collection="EO:EUM:DAT:0662",
            enabled=False,
        ),
        SourceDefinition(
            id="dummy",
            driver="dummy",
            label="Testquelle (Platzhalterbilder)",
            enabled=False,
        ),
    ]


class SourcesConfig(BaseModel):
    catalog: list[SourceDefinition] = Field(default_factory=default_catalog)
    poll_interval_minutes: int = Field(default=15, gt=0)
    # Abgleich des Katalogs mit dem EUMETSAT Data Store; 0 = aus.
    auto_sync_hours: int = Field(default=24, ge=0)

    @model_validator(mode="before")
    @classmethod
    def _migrate_active_list(cls, data: Any) -> Any:
        """Alte Konfigurationen hatten ``active: [..]`` statt eines Katalogs."""
        if isinstance(data, dict) and "active" in data:
            data = dict(data)
            active = data.pop("active") or []
            if "catalog" not in data:
                catalog = default_catalog()
                for entry in catalog:
                    entry.enabled = entry.id in active
                data["catalog"] = [entry.model_dump() for entry in catalog]
        return data

    @model_validator(mode="after")
    def _unique_ids(self) -> "SourcesConfig":
        seen: set[str] = set()
        for entry in self.catalog:
            if entry.id in seen:
                raise ValueError(f"Quellen-ID '{entry.id}' ist im Katalog doppelt vergeben")
            seen.add(entry.id)
        return self

    def get(self, source_id: str) -> SourceDefinition | None:
        for entry in self.catalog:
            if entry.id == source_id:
                return entry
        return None

    def is_enabled(self, source_id: str) -> bool:
        entry = self.get(source_id)
        return bool(entry and entry.enabled)


class HistoryConfig(BaseModel):
    history_minutes: int = Field(default=DEFAULT_HISTORY_MINUTES, gt=0)
    max_storage_mb: int = Field(default=500, gt=0)


class StorageConfig(BaseModel):
    # Leer = Standard (<Datenverzeichnis>/frames, also im /data-Volume).
    frames_dir: str = ""

    @field_validator("frames_dir")
    @classmethod
    def _validate_frames_dir(cls, value: str) -> str:
        value = value.strip()
        if value and not value.startswith("/"):
            raise ValueError("Der Speicherort muss ein absoluter Pfad sein (z. B. /mnt/data/ha_satellite)")
        return value.rstrip("/") or ("/" if value else "")


class ServerConfig(BaseModel):
    mjpeg_fps: float = Field(default=2.0, gt=0)


class AppConfig(BaseModel):
    eumetsat: EumetsatCredentials = Field(default_factory=EumetsatCredentials)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    history: HistoryConfig = Field(default_factory=HistoryConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    regions: list[RegionConfig] = Field(default_factory=list)

    @model_validator(mode="after")
    def _regions_reference_catalog(self) -> "AppConfig":
        known = [entry.id for entry in self.sources.catalog]
        for region in self.regions:
            if region.source not in known:
                raise ValueError(
                    f"Region '{region.name}' verweist auf unbekannte Quelle '{region.source}'. "
                    f"Im Katalog: {', '.join(known) or '-'}"
                )
        return self

    def max_frames_for(self, region: RegionConfig) -> int:
        return region.effective_max_frames(
            self.sources.poll_interval_minutes, self.history.history_minutes
        )

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
