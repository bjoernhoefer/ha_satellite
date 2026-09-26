# AGENTS.md — Betriebsanleitung ha_satellite

> **PFLICHT bei JEDER Änderung (Code, Template, Config, Docker):**
> Vor dem Commit die Klicktests der Web-UI **ausführen und grün haben**
> (`pytest -q`, inkl. `tests/test_ui.py` mit Playwright - nicht übersprungen!).
> Neue oder geänderte UI-Funktionen bekommen **im selben Commit** einen
> eigenen Klicktest (Desktop, und bei Anzeige-/Bedienthemen zusätzlich die
> Handy-Ansicht über die `mobile_page`-Fixture). Details unter
> [Tests](#tests).

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
  config.py       Konfigurationsmodell (Pydantic) + YAML-Persistenz + Env-Override,
                   Quellen-Katalog, Speicherort, Historie
  geometry.py      Bounding-Box-Berechnung aus Mittelpunkt + Umkreis
  buffer.py        Ringpuffer (Frames pro Region, Aufräumen, Speicherlimit, Umzug)
  status.py        Status-Speicher (letzter/nächster Lauf, Fehler, Frame-Anzahl)
  scheduler.py     APScheduler-Jobs (nicht-blockierend), Single-Render-Lock,
                   Quellen-Abgleich-Job
  sources/         Treiber: msg_seviri, data_tailor, mtg_fci (+ dummy)
  source_sync.py   Abgleich des Katalogs mit dem EUMETSAT Data Store (öffentliche API)
  storage.py       Speicherort-Kandidaten, Schreibtest, System-Disk-/Container-Warnung
  logbuffer.py     In-Memory-Log (für die UI) + rotierende Logdatei
  main.py          FastAPI-App: Web-UI + REST-API + Bild-Endpunkte
  templates/       Jinja2-Template der Web-UI (inkl. Vollbild-Betrachter)
```

Zustand liegt unter `/data` (Konfiguration, `source_sync.json`, `logs/`)
und am konfigurierten Bilder-Speicherort (Default `/data/frames`, in der UI
änderbar, z. B. `/mnt/data/ha_satellite`). Der Container selbst ist
zustandslos.

### Quellen-Katalog

`sources.catalog` in `config.yaml` legt fest, welche Quellen zur Verfügung
stehen (`id`, `driver`, `label`, `collection`, `enabled`, `description`).
Mehrere Einträge dürfen denselben Treiber nutzen (z. B. `msg_seviri` für
0°, Rapid Scan, IODC). Regionen verweisen über `source` auf eine `id`; nur
Regionen mit aktivierter Quelle werden automatisch abgerufen. In der UI ist
der Katalog als Tabelle (Aktiv-Schalter) und als einklappbares JSON
editierbar. Alte Konfigurationen mit `sources.active: [...]` werden beim
Laden automatisch migriert.

Der **Quellen-Abgleich** (`source_sync.py`, Button „Jetzt abgleichen“ bzw.
automatisch alle `sources.auto_sync_hours` Stunden, 0 = aus) fragt die
öffentlichen Browse-/Search-Endpunkte des EUMETSAT Data Store ab: ob die
Collection existiert, Zeitpunkt des letzten Produkts, und neu verfügbare,
von einem Treiber unterstützte Collections („entdeckt“, per Klick
übernehmbar - landen deaktiviert im Katalog). Ergebnis in
`/data/source_sync.json`, getrennt von der Konfiguration.

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

- Konfiguration/Logs liegen unter `./data` (Host) → `/data` (Container).
  Das `docker/entrypoint.sh`-Skript gleicht beim Start die Ownership von
  `/data` (und des gewählten Bilder-Speicherorts) automatisch auf den
  internen Nutzer an (siehe HISTORY.md, "Bind-Mount-Ownership").
- **Bilder-Speicherort:** Compose hängt `/mnt` des Hosts unter `/mnt` ein;
  in der Web-UI („Speicherort & Historie“) wird z. B.
  `/mnt/data/ha_satellite` gewählt, vorhandene Bilder werden auf Wunsch
  verschoben. Der Dienst läuft als `PUID`/`PGID` (Default 1000) - das
  Zielverzeichnis muss dafür beschreibbar sein, einmalig z. B.
  `sudo install -d -o 1000 -g 1000 /mnt/data/ha_satellite`. Die UI warnt,
  wenn ein Pfad auf demselben Datenträger wie `/data` (System-Disk) oder nur
  im Container-Dateisystem (nicht persistent) liegt.
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
- Die Region-Jobs starten um je 60 s versetzt, damit sie nicht dauerhaft am
  globalen Render-Lock kollidieren.
- Ein Speicherort-Wechsel hält den Render-Lock (`scheduler.exclusive()`),
  prüft den Zielpfad per Schreibtest und führt beim Verschieben die
  Ringpuffer-Indizes zusammen.
- **Logs** sind zentral: Render-Start/-Ende, übersprungene Läufe, Fehler,
  Konfigurations- und Speicherort-Änderungen, Quellen-Abgleich werden auf
  INFO/WARNING geloggt. Die UI zeigt sie ganz unten live an
  (`GET /api/logs`), zusätzlich rotierend in `/data/logs/ha_satellite.log`.
  Neue Funktionen müssen ihre relevanten Schritte ebenfalls loggen.
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
| `GET /api/regions/{region}/frames` | Historie: Frames im Puffer (neuester zuerst) mit stabiler URL |
| `GET /api/sources` | Quellen-Katalog + Ergebnis des letzten Abgleichs |
| `POST /api/sources/sync` | Abgleich mit dem EUMETSAT Data Store jetzt ausführen |
| `POST /api/sources/adopt` | Entdeckte Collection (`{"collection": ...}`) in den Katalog übernehmen |
| `GET /api/storage` | Aktueller Speicherort, Belegung, Kandidaten mit freiem Platz/Warnungen |
| `POST /api/storage` | Speicherort/Historie setzen (`frames_dir`, `move_existing`, `history_minutes`, `max_storage_mb`) |
| `GET /api/logs` | Log-Einträge (`?after=<id>`, `?format=text`) |
| `GET /live/{region}` | Merkbare Adresse: öffnet den Live-Stream im Browser |
| `GET /healthz` | Liveness |
| `GET /regions/{region}/latest.png` | Neuestes Einzelbild |
| `GET /regions/{region}/frames/{i}.png` | Frame `i` aus dem Puffer (`0` = neuester) |
| `GET /regions/{region}/history/{datei}` | Frame per Dateiname (`?w=240` = JPEG-Vorschaubild) |
| `GET /regions/{region}/animation.gif` | Animation der Historie (GIF) |
| `GET /regions/{region}/animation.mp4` | dito als MP4 (benötigt `imageio-ffmpeg`) |
| `GET /regions/{region}/mjpeg` | MJPEG-Stream, loopt die Frames (Home-Assistant-Kamera) |

## Tests

```bash
pip install -e ".[dev,ui]"
playwright install chromium   # einmalig, für die UI-Klicktests
pytest -q
```

Abgedeckt: Konfigurations-Handling (Laden/Speichern, Env-Override, Maskierung,
Quellen-Katalog, Migration), Ringpuffer-Logik (Max-Frames, Speicherlimit,
Waisen-Aufräumen, Umzug), Bounding-Box-Berechnung, die APIs gegen einen
echten uvicorn-Prozess (`tests/test_api_config.py`,
`tests/test_api_features.py`, EUMETSAT per lokalem Fake-Server) sowie
**Browser-Klicktests der Web-UI** (`tests/test_ui.py`, Playwright).

**Klicktests sind bei jeder Änderung Pflicht** (siehe Hinweis ganz oben).
Sie sichern die Grundfunktionen ab: Zugangsdaten eintragen/speichern,
Secret bleibt maskiert und beim Speichern ohne Neueingabe erhalten,
Env-Override sperrt die Felder, Intervall speichern, "Jetzt aktualisieren"
ohne Seitenwechsel, Vorschau anklicken → Vollbild-Betrachter füllt den
Viewport (Desktop **und** Handy-Viewport 390×844 mit Touch, Zurück-Taste
schließt), Historie blättern/Zeitraffer, Live-Stream im Browser inkl.
Deep-Link `/live/{region}`, Logs ganz unten, Quellen-JSON (eingeklappt,
editierbar, Validierung), Quellen aktivieren, Abgleich + Übernehmen,
Speicherort wechseln inkl. Verschieben, kein horizontales Scrollen am Handy.
Selektoren laufen ausschließlich über `data-testid` - Markup und Styling
dürfen sich frei ändern, die IDs müssen erhalten bleiben. Ohne installiertes
Playwright werden die Klicktests lokal übersprungen; in CI erzwingt
`HA_SATELLITE_REQUIRE_UI_TESTS=1`, dass sie laufen.
