# ha_satellite

Containerisierter Dienst, der EUMETSAT-Satellitenbilder herunterlädt, zu
Bildausschnitten rendert und sie **Home Assistant** als Kamera-Entitäten
bereitstellt (Standbild, Historie, MJPEG-Loop, GIF/MP4-Animation).

> **Status:** Grundgerüst (Phase 1). Konfiguration, Web-Server, API,
> Ringpuffer und alle Endpunkte sind lauffähig; die Bilder sind aktuell
> generierte **Platzhalter** ("Dummy-Frames") mit Zeitstempel, Region und
> Bounding-Box. Die echte EUMETSAT/Satpy-Rendering-Kette folgt in Phase 2
> (siehe [HISTORY.md](HISTORY.md) und [AGENTS.md](AGENTS.md)).

## Schnellstart

```bash
git clone https://github.com/bjoernhoefer/ha_satellite.git
cd ha_satellite
cp .env.example .env   # optional: EUMETSAT-Zugangsdaten per Env setzen
docker compose up -d --build
```

Danach:

- Web-UI: http://\<host\>:6060/
- Status-API: http://\<host\>:6060/api/status
- Beispiel-Kamera (Wien): http://\<host\>:6060/regions/wien/mjpeg

Die EUMETSAT-Zugangsdaten (Consumer Key + Secret) können entweder in der
Web-UI eingegeben (wird nach `/data/config.yaml` persistiert) oder per
Umgebungsvariable (`EUMETSAT_CONSUMER_KEY` / `EUMETSAT_CONSUMER_SECRET`,
siehe `.env.example`) gesetzt werden. Die Env-Variablen haben Vorrang.

## Deployment mit vorgebautem Image

Auf dem Zielhost wird kein Quellcode benötigt — nur die
`docker-compose.yml`. Da das Repository privat ist, ist auch das
GHCR-Paket privat und der Host braucht einmalig einen Login mit einem
Personal Access Token (Scope `read:packages`):

```bash
echo '<PAT>' | docker login ghcr.io -u <github-user> --password-stdin

mkdir -p ~/ha_satellite && cd ~/ha_satellite
curl -sO https://raw.githubusercontent.com/bjoernhoefer/ha_satellite/main/docker-compose.yml
docker compose up -d
```

Beim Deployment ohne Quellcode sollte der `build:`-Block aus der
`docker-compose.yml` entfernt und stattdessen `pull_policy: always`
gesetzt werden, damit Compose nicht lokal zu bauen versucht.

## Funktionsumfang

- **Quellen** (austauschbar je Region): `msg_seviri` (Full Disk + lokales
  Satpy-Rendering, Default), `data_tailor` (serverseitiger Zuschnitt bei
  EUMETSAT), `mtg_fci` (höher aufgelöste Chunks).
- **Regionen**: Name, Mittelpunkt (Lat/Lon), Umkreis in km (daraus wird die
  Bounding-Box berechnet), Ausgabegröße, Komposit, Quelle. Vorkonfiguriert:
  `wien` und `mallorca`.
- **Historie**: Rollierender Ringpuffer pro Region unter
  `/data/frames/{region}/`, Anzahl konfigurierbar (Default ≈ 60 Minuten
  Historie, abhängig vom Abrufintervall), zusätzlich Speicher-Limit (MB)
  als Notbremse. Alte Frames und Waisen werden automatisch aufgeräumt.
- **Web-API** (siehe [AGENTS.md](AGENTS.md) für Details):
  `GET/POST /api/config`, `GET /api/status`, `GET /healthz`,
  `GET /regions/{region}/latest.png`, `GET /regions/{region}/frames/{i}.png`,
  `GET /regions/{region}/animation.gif`, `GET /regions/{region}/animation.mp4`,
  `GET /regions/{region}/mjpeg`.
- **Konfigurations-UI** unter `/`: Zugangsdaten (Secret maskiert), Regionen,
  aktive Quellen, Abrufintervall, Status mit Vorschaubild, Button
  „Jetzt aktualisieren“.

## Home Assistant einbinden

Fertige, kopierbare YAML-Schnipsel für Wien und Porto Cristo (via
WireGuard) stehen in [docs/homeassistant.md](docs/homeassistant.md).

## Entwicklung

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,ui]"
playwright install chromium   # für die UI-Klicktests
pytest -q
uvicorn ha_satellite.main:app --reload --port 6060
```

Details zu Architektur, Deployment und Betriebsregeln: [AGENTS.md](AGENTS.md).
Chronik und Fallstricke: [HISTORY.md](HISTORY.md).
