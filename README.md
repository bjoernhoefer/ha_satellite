# ha_satellite

Containerisierter Dienst, der EUMETSAT-Satellitenbilder herunterlädt, zu
Bildausschnitten rendert und sie **Home Assistant** als Kamera-Entitäten
bereitstellt (Standbild, Historie, MJPEG-Loop, GIF/MP4-Animation).

> **Status:** Phase 2. Die Quelle `msg_seviri` liefert echte Bilder aus
> dem MSG-SEVIRI-Rapid-Scan (alle 5 Minuten, Europa), gerendert mit Satpy;
> `data_tailor` und `mtg_fci` liefern noch Platzhalterbilder
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
- Live-Ansicht im Browser (auch am Handy): http://\<host\>:6060/live/wien

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

- **Quellen-Katalog**: welche Quellen zur Verfügung stehen, ist in der UI
  einstellbar (Tabelle mit Aktiv-Schalter + einklappbarer JSON-Editor).
  Treiber: `msg_seviri` (Rapid Scan Europa aus dem EUMETSAT Data Store +
  lokales Satpy-Rendering, Default; ein Download pro Aufnahme für alle
  Regionen, ~100 MB, d. h. bei 15 min Intervall ~9 GB/Tag, bei 5 min
  ~28 GB/Tag), `data_tailor` (serverseitiger Zuschnitt bei EUMETSAT),
  `mtg_fci` (höher aufgelöste Chunks). Ein automatischer Abgleich mit dem
  EUMETSAT Data Store prüft Verfügbarkeit/Aktualität und schlägt neue
  Collections vor.
- **Regionen**: Name, Mittelpunkt (Lat/Lon), Umkreis in km (daraus wird die
  Bounding-Box berechnet), Ausgabegröße, Komposit, Quelle. Vorkonfiguriert:
  `wien` und `mallorca` mit dem Komposit `natural_color_raw_with_night_ir`
  (tagsüber Echtfarben, nachts IR-Wolken; weitere siehe AGENTS.md).
- **Historie**: Rollierender Ringpuffer pro Region unter
  `<Speicherort>/{region}/`, Dauer in der UI einstellbar (Default 60
  Minuten), zusätzlich Speicher-Limit (MB) als Notbremse. Alte Frames und
  Waisen werden automatisch aufgeräumt.
- **Speicherort** der Bilder in der UI wählbar (z. B. `/mnt/data/ha_satellite`,
  `docker-compose.yml` hängt dafür `/mnt` ein), inkl. Verschieben vorhandener
  Bilder und Warnung bei System-Disk oder nicht persistentem Pfad.
- **Web-API** (siehe [AGENTS.md](AGENTS.md) für Details):
  `GET/POST /api/config`, `GET /api/status`, `GET /healthz`,
  `GET /regions/{region}/latest.png`, `GET /regions/{region}/frames/{i}.png`,
  `GET /regions/{region}/animation.gif`, `GET /regions/{region}/animation.mp4`,
  `GET /regions/{region}/mjpeg`.
- **Web-UI** unter `/` (auch fürs Handy): Vorschaubilder und Historie
  anklickbar → Vollbild-Betrachter (Blättern per Pfeiltasten/Wischen,
  Zeitraffer), Live-Stream im Browser, Quellen, Speicherort, Zugangsdaten
  (Secret maskiert), Abrufintervall, Status und **Live-Logs** ganz unten.

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
