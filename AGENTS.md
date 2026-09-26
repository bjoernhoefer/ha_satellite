# AGENTS.md — Betriebsanleitung ha_satellite

## Zielumgebung

| Punkt | Wert |
|---|---|
| Host | `nzbpi` / `192.168.188.13`, Raspberry Pi 5, ARM64, Debian Bookworm |
| Betrieb | Docker Compose, neben bestehendem `grafana`-Container |
| Konsumenten | HA Wien (`192.168.188.12:8123`), HA Porto Cristo (`192.168.88.77:8123`) |
| Netz | Beide Standorte per WireGuard direkt erreichbar |
| Auth | Keine — nur privates Netz |
| Web-Port | `6060` |

Die Integration erfolgt **pro Standort in der jeweiligen HA-Instanz** (siehe
`docs/homeassistant.md`), nicht über Grafana. Grafana bleibt unangetastet.

## Architektur

```
src/ha_satellite/
  config.py       Konfigurationsmodell (Pydantic) + YAML-Persistenz + Env-Override
  geometry.py      Bounding-Box-Berechnung aus Mittelpunkt + Umkreis
  buffer.py        Ringpuffer (Frames pro Region, Aufräumen, Speicherlimit)
  status.py        Status-Speicher (letzter/nächster Lauf, Fehler, Frame-Anzahl)
  scheduler.py     APScheduler-Jobs (nicht-blockierend), Single-Render-Lock
  sources/         Source-Abstraktion: msg_seviri, data_tailor, mtg_fci (+ dummy)
    eumetsat.py      Data-Store-Suche + Download (eumdac), Produkt-Cache /data/cache
    satpy_render.py  Satpy-Rendering im Kindprozess (Crop-Fenster, Resampling, PNG)
  main.py          FastAPI-App: Web-UI + REST-API + Bild-Endpunkte
  templates/       Jinja2-Template für die Konfigurations-UI
```

Zustand (Konfiguration, Frames, Produkt-Cache) liegt ausschließlich unter `/data`
(gemountetes Volume). Der Container selbst ist zustandslos.

### Phasenmodell

- **Phase 1 (abgeschlossen):** Grundgerüst — Konfiguration, Web-Server, API,
  Ringpuffer, Scheduler, alle Endpunkte funktionieren End-to-End, liefern
  aber generierte Platzhalterbilder ("Dummy-Frames"). So lässt sich der
  ARM64-Build und die gesamte Infrastruktur früh verifizieren, bevor die
  deutlich komplexere Satpy/eumdac-Kette dazukommt.
- **Phase 2 (dieser Stand):** `msg_seviri` liefert echte Bilder: neuestes
  Produkt der Collection `sources.msg_collection` (Default
  `EO:EUM:DAT:MSG:MSG15-RSS`, Rapid Scan Europa, alle 5 min) wird per
  `eumdac` geladen (nur die `.nat`-Datei, ~100 MB), **einmal pro Produkt**
  in `/data/cache/` abgelegt und von allen Regionen genutzt. Gerendert wird
  mit Satpy (`seviri_l1b_native`) in einem **Kindprozess** (Speicher wird
  danach vollständig freigegeben; ein OOM trifft nicht den Webserver).
  Frame-Zeitstempel = Aufnahmeende. `data_tailor`/`mtg_fci` liefern noch
  Dummy-Frames (Folgeschritt, FCI-Produkte sind ~1 GB/Slot → Data Tailor).
- `Source.render(region, config, last_sensing) -> RenderedFrame`: ist die
  neueste Aufnahme nicht jünger als `last_sensing` (neuester Frame im
  Puffer), wird `NoNewData` ausgelöst — kein Fehler, kein doppelter Frame.

### Komposite (msg_seviri)

Default `natural_color_raw_with_night_ir` (tagsüber Echtfarben, nachts
IR-Wolken). Weitere funktionierende Satpy-Komposite: `natural_color`
(nachts schwarz), `convection`, `airmass`, `cloudtop`,
`colorized_ir_clouds`. **Nicht** verwenden: `natural_color_with_night_ir`
(lädt zur Laufzeit NASA-BlackMarble nach, scheitert am Hash). Unbekannte
Namen führen zu einem Fehler im Status, nicht zum Absturz. Richtwerte auf
dem Pi 5: ~6 s und ~570 MB Spitzen-RSS pro Region und Lauf.

## ARM64-Build-Entscheidung

Der Pi 5 ist ARM64. Satpys Abhängigkeiten `pyresample`, `pykdtree` und
`pyproj` bauen aus dem PyPI-Sdist heraus auf ARM sehr langsam (native
Erweiterungen, PROJ-C-Library) und benötigen zusätzliche System-Header.
`mambaorg/micromamba` mit dem `conda-forge`-Kanal bietet fertige
`linux-aarch64`-Builds für genau diese Pakete. Deshalb basiert das
`docker/Dockerfile` auf micromamba. **Stand Phase 2:** conda-forge führt
keine aktuelle `trollimage`-Version für linux-aarch64 (Satpy ließ sich
nicht lösen), während PyPI inzwischen manylinux-aarch64-Wheels für
`trollimage`, `pyresample`, `pykdtree`, `pyproj` und `numpy` hat. conda
liefert daher nur Python, die Satpy-Kette kommt per `pip install
--only-binary=…` (kein Sdist-Build; fehlt ein Wheel, bricht der Build ab).
Build auf dem Pi 5: ~3 min, Image ~1,1 GB. Alternativen (u. a. `python:3.12-slim` + `apt`-Header + pip,
oder vortrainierte Wheels von piwheels) wurden verworfen, da sie entweder
sehr lange Build-Zeiten auf dem Pi 5 selbst (kein Cross-Compile im CI)
oder zusätzliche Wartungslast durch manuelles Wheel-Pinning bedeutet hätten.

## Deployment

```bash
cd /opt/ha_satellite   # oder Ziel-Verzeichnis auf nzbpi
git pull
docker compose pull    # oder --build, falls lokal gebaut wird
docker compose up -d
```

- Konfiguration/Frames liegen unter `./data` (Host) → `/data` (Container).
  Das `docker/entrypoint.sh`-Skript gleicht beim Start die Ownership von
  `/data` automatisch auf den internen Nutzer an (siehe HISTORY.md,
  "Bind-Mount-Ownership") - kein manuelles `chown` auf dem Host nötig.
- Multi-Arch-Image (`linux/amd64`, `linux/arm64`) wird von
  `.github/workflows/build.yml` gebaut und nach
  `ghcr.io/bjoernhoefer/ha_satellite` gepusht (bei Push auf `main` bzw. Tags).
- Memory-Limit im Compose (`deploy.resources.limits.memory: 768M`), da sich
  der Pi 5 die Ressourcen mit `grafana` teilt.

## Betriebsregeln

- **Ein Render-Vorgang zur Zeit**, systemweit (nicht nur pro Region) — via
  `threading.Lock` in `RenderScheduler`. Kollidierende Läufe werden
  übersprungen und beim nächsten Intervall erneut versucht.
- Download/Rendering laufen über APScheduler in einem eigenen Thread-Pool
  und blockieren den asyncio-Webserver nicht.
- Fehler (fehlende Slots, Timeouts, ungültige Credentials) werden geloggt
  und im `StatusStore` vermerkt (`GET /api/status`), führen aber **nicht**
  zum Absturz; der nächste geplante Lauf ist der Retry.
- Ringpuffer-Index (`_index.json`) pro Region hält die Reihenfolge fest;
  Frame-Index `0` = neuester Frame. Beim Start wird die Platte mit dem
  Index abgeglichen: fehlende Dateien werden aus dem Index entfernt, Dateien
  ohne Index-Eintrag ("Waisen") gelöscht.
- Konfigurationsänderungen über `POST /api/config` lösen sofort ein
  Neuaufsetzen der Scheduler-Jobs aus (`scheduler.reload()`).
- **Keine Geheimnisse ins Repo.** `.env.example` dokumentiert die
  verfügbaren Variablen, echte Werte nur in `.env` (gitignored) oder in der
  Web-UI unter `/data/config.yaml`.

## API-Übersicht

| Methode/Pfad | Zweck |
|---|---|
| `GET /` | Konfigurations-UI |
| `GET /api/config` | Aktuelle Konfiguration (Secret maskiert) |
| `POST /api/config` | Konfiguration speichern (Merge auf gespeicherte Werte; leeres oder maskiertes Secret = unverändert) |
| `GET /api/status` | Letzter/nächster Lauf, Frame-Anzahl, Alter, Fehler je Region |
| `POST /api/regions/{region}/refresh` | Sofortiger Render-Lauf ("Jetzt aktualisieren") |
| `GET /healthz` | Liveness |
| `GET /regions/{region}/latest.png` | Neuestes Einzelbild |
| `GET /regions/{region}/frames/{i}.png` | Frame `i` aus dem Puffer (`0` = neuester) |
| `GET /regions/{region}/animation.gif` | Animation der Historie (GIF) |
| `GET /regions/{region}/animation.mp4` | dito als MP4 (benötigt `imageio-ffmpeg`) |
| `GET /regions/{region}/mjpeg` | MJPEG-Stream, loopt die Frames (Home-Assistant-Kamera) |

## Tests

```bash
pip install -e ".[dev,ui]"
playwright install chromium   # einmalig, für die UI-Klicktests
pytest -q
```

`eumdac` und der Satpy-Kindprozess sind in `tests/test_sources.py` gemockt;
die Test-Server aus `tests/conftest.py` starten mit Regionen auf der
`dummy`-Quelle, sodass kein Test den echten Data Store anspricht.

Abgedeckt: Konfigurations-Handling (Laden/Speichern, Env-Override, Maskierung),
Ringpuffer-Logik (Max-Frames, Speicherlimit, Waisen-Aufräumen),
Bounding-Box-Berechnung, die Config-API gegen einen echten uvicorn-Prozess
(`tests/test_api_config.py`) sowie **Browser-Klicktests der Web-UI**
(`tests/test_ui.py`, Playwright).

**Regeln für UI-Änderungen:** Die Klicktests sind die Absicherung der
Grundfunktionen (Zugangsdaten eintragen/speichern, Secret bleibt maskiert
und bleibt beim Speichern ohne Neueingabe erhalten, Env-Override sperrt die
Felder, Intervall speichern, "Jetzt aktualisieren" ohne Seitenwechsel).
Selektoren laufen ausschließlich über `data-testid` - Markup und Styling
dürfen sich frei ändern, die IDs müssen erhalten bleiben. Ohne installiertes
Playwright werden die Klicktests lokal übersprungen; in CI erzwingt
`HA_SATELLITE_REQUIRE_UI_TESTS=1`, dass sie laufen.
