# AGENTS.md — Betriebsanleitung ha_satellite

## Zielumgebung

| Punkt | Wert |
|---|---|
| Host | `nzbpi` / `192.168.188.13`, Raspberry Pi 5, ARM64, Debian Bookworm |
| Betrieb | Docker Compose, neben bestehendem `grafana`-Container |
| Konsumenten | HA Wien (`192.168.188.12:8123`), HA Porto Cristo (`192.168.88.77:8123`) |
| Netz | Beide Standorte per WireGuard direkt erreichbar |
| Auth | Keine — nur privates Netz |
| Web-Port | `8080` |

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
  main.py          FastAPI-App: Web-UI + REST-API + Bild-Endpunkte
  templates/       Jinja2-Template für die Konfigurations-UI
```

Zustand (Konfiguration + Frames) liegt ausschließlich unter `/data`
(gemountetes Volume). Der Container selbst ist zustandslos.

### Phasenmodell

- **Phase 1 (dieser Stand):** Grundgerüst — Konfiguration, Web-Server, API,
  Ringpuffer, Scheduler, alle Endpunkte funktionieren End-to-End, liefern
  aber generierte Platzhalterbilder ("Dummy-Frames"). So lässt sich der
  ARM64-Build und die gesamte Infrastruktur früh verifizieren, bevor die
  deutlich komplexere Satpy/eumdac-Kette dazukommt.
- **Phase 2 (folgt):** Echte Implementierung von `MsgSeviriSource`,
  `DataTailorSource`, `MtgFciSource` in `src/ha_satellite/sources/` (Download
  via `eumdac`, Rendering via Satpy). Die Schnittstelle (`Source.render()`)
  ändert sich dabei nicht — nur der Rumpf der jeweiligen `render()`-Methode
  wird ausgetauscht.

## ARM64-Build-Entscheidung

Der Pi 5 ist ARM64. Satpys Abhängigkeiten `pyresample`, `pykdtree` und
`pyproj` bauen aus dem PyPI-Sdist heraus auf ARM sehr langsam (native
Erweiterungen, PROJ-C-Library) und benötigen zusätzliche System-Header.
`mambaorg/micromamba` mit dem `conda-forge`-Kanal bietet fertige
`linux-aarch64`-Builds für genau diese Pakete. Deshalb basiert das
`docker/Dockerfile` bereits jetzt auf micromamba, obwohl Phase 1 nur reine
Python-Pakete benötigt — so ist der Wechsel des Basis-Images beim Umstieg
auf Satpy in Phase 2 nicht mehr nötig, und der ARM64-Build ist von Anfang an
verifiziert. Alternativen (u. a. `python:3.12-slim` + `apt`-Header + pip,
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
| `POST /api/config` | Konfiguration speichern (Merge auf bestehende Werte) |
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
pip install -e ".[dev]"
pytest -q
```

Abgedeckt: Konfigurations-Handling (Laden/Speichern, Env-Override, Maskierung),
Ringpuffer-Logik (Max-Frames, Speicherlimit, Waisen-Aufräumen) und
Bounding-Box-Berechnung.
