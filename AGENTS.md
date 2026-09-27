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
  overlay.py       Landesgrenzen-Overlay (Natural Earth 1:10m, overlay_data/)
  buffer.py        Ringpuffer (Frames pro Region, Aufräumen, Speicherlimit, Umzug)
  status.py        Status-Speicher (letzter/nächster Lauf, Fehler, Frame-Anzahl)
  scheduler.py     APScheduler-Jobs (nicht-blockierend): Download je Quelle im
                   Aufnahmetakt, Rendern nach neuer Aufnahme, Single-Render-Lock,
                   Quellen-Abgleich-Job
  media.py         Gecachte Vorschaubilder/JPEGs und Animationen (GIF/MP4)
  sources/         Treiber: msg_seviri, data_tailor, mtg_fci (+ dummy)
    eumetsat.py      Data-Store-Suche + Download (eumdac), Produkt-Cache /data/cache
    fci_archive.py   MTG-FCI-Rohdaten-Archiv: Chunk-Geometrie, Download je Slot, Aufräumen
    satpy_render.py  Satpy-Rendering im Kindprozess (Crop-Fenster, Resampling, PNG)
    hrv_composite.py Eigener Compositor: Echtfarben mit HRV geschärft
  satpy_config/    Eigene Satpy-Komposit-Definitionen (composites/seviri.yaml, visir.yaml)
  source_sync.py   Abgleich des Katalogs mit dem EUMETSAT Data Store (öffentliche API)
  storage.py       Speicherort-Kandidaten, Schreibtest, System-Disk-/Container-Warnung
  logbuffer.py     In-Memory-Log (für die UI) + rotierende Logdatei
  main.py          FastAPI-App: Web-UI + REST-API + Bild-Endpunkte
  templates/       Jinja2-Template der Web-UI (inkl. Vollbild-Betrachter)
```

Zustand liegt unter `/data` (Konfiguration, `source_sync.json`, `logs/`,
Produkt-Cache `cache/`)
und am konfigurierten Bilder-Speicherort (Default `/data/frames`, in der UI
änderbar, z. B. `/mnt/data/ha_satellite`; dort auch das FCI-Archiv unter
`_archive/`). Der Container selbst ist zustandslos. Regionsnamen dürfen
nicht mit `_` beginnen (reserviert für interne Verzeichnisse).

### Quellen-Katalog

`sources.catalog` in `config.yaml` legt fest, welche Quellen zur Verfügung
stehen (`id`, `driver`, `label`, `collection`, `enabled`, `description`).
Optional `cycle_minutes` (Aufnahmetakt, siehe
„Download im Aufnahmetakt“). Mehrere Einträge dürfen denselben Treiber nutzen (z. B. `msg_seviri` für
0°, Rapid Scan, IODC). Regionen verweisen über `source` auf eine `id`; nur
Regionen mit aktivierter Quelle werden automatisch abgerufen. In der UI ist
der Katalog als Tabelle (Aktiv-Schalter) und als einklappbares JSON
editierbar. Die `collection` eines `msg_seviri`-Eintrags bestimmt, welche
Data-Store-Collection geladen wird (Default-Eintrag `msg_seviri` = Rapid
Scan `EO:EUM:DAT:MSG:MSG15-RSS`). Alte Konfigurationen mit
`sources.active: [...]` bzw. `sources.msg_collection` werden beim Laden
automatisch migriert.

Der **Quellen-Abgleich** (`source_sync.py`, Button „Jetzt abgleichen“ bzw.
automatisch alle `sources.auto_sync_hours` Stunden, 0 = aus) fragt die
öffentlichen Browse-/Search-Endpunkte des EUMETSAT Data Store ab: ob die
Collection existiert, Zeitpunkt des letzten Produkts, und neu verfügbare,
von einem Treiber unterstützte Collections („entdeckt“, per Klick
übernehmbar - landen deaktiviert im Katalog). Ergebnis in
`/data/source_sync.json`, getrennt von der Konfiguration.

### Phasenmodell

- **Phase 1 (abgeschlossen):** Grundgerüst — Konfiguration, Web-Server, API,
  Ringpuffer, Scheduler, alle Endpunkte funktionieren End-to-End, liefern
  aber generierte Platzhalterbilder ("Dummy-Frames"). So lässt sich der
  ARM64-Build und die gesamte Infrastruktur früh verifizieren, bevor die
  deutlich komplexere Satpy/eumdac-Kette dazukommt.
- **Phase 2 (abgeschlossen):** `msg_seviri` liefert echte Bilder: neuestes
  Produkt der Collection `sources.msg_collection` (Default
  `EO:EUM:DAT:MSG:MSG15-RSS`, Rapid Scan Europa, alle 5 min) wird per
  `eumdac` geladen (nur die `.nat`-Datei, ~100 MB), **einmal pro Produkt**
  in `/data/cache/` abgelegt und von allen Regionen genutzt. Gerendert wird
  mit Satpy (`seviri_l1b_native`) in einem **Kindprozess** (Speicher wird
  danach vollständig freigegeben; ein OOM trifft nicht den Webserver).
  Frame-Zeitstempel = Aufnahmeende. `data_tailor` liefert noch
  Dummy-Frames.
- **Phase 3 (dieser Stand):** `mtg_fci` liefert echte Bilder aus MTG FCI
  (1 km sichtbar, 2 km IR) über ein Rohdaten-Archiv, siehe unten.
- `Source.render(region, config, last_sensing) -> RenderedFrame`: ist die
  neueste Aufnahme nicht jünger als `last_sensing` (neuester Frame im
  Puffer), wird `NoNewData` ausgelöst — kein Fehler, kein doppelter Frame.

### Komposite (msg_seviri)

Default `natural_color_hrv_with_night_ir` (tagsüber Echtfarben, mit dem
~1-km-HRV-Kanal geschärft; nachts IR-Wolken). Eigene Komposite liegen in
`src/ha_satellite/satpy_config/composites/seviri.yaml` (Compositor in
`sources/hrv_composite.py`) und werden im Kindprozess per
`satpy.config config_path` eingebunden. Weitere funktionierende Komposite:
`natural_color_hrv` (nur Tag), `natural_color_raw_with_night_ir` (bisheriger
Default, ~3 km), `natural_color` (nachts schwarz), `convection`, `airmass`,
`cloudtop`, `colorized_ir_clouds`, `hrv_clouds`. **Nicht** verwenden:
`natural_color_with_night_ir` (lädt zur Laufzeit NASA-BlackMarble nach,
scheitert am Hash). Unbekannte Namen führen zu einem Fehler im Status,
nicht zum Absturz. Komposite aus Kanälen unterschiedlicher Auflösung
entstehen erst beim Resampling: jeder Kanal wird mit eigenem Fenster
zugeschnitten, geladen wird mit `generate=False` (siehe HISTORY.md,
"HRV-Schärfung"). Funktioniert für Rapid Scan und 0° (Full Disk).
Richtwerte auf dem Pi 5: ~8–9 s und ~400–450 MB Spitzen-RSS pro Region und
Lauf.

### Landesgrenzen-Overlay

Pro Region `borders: true` (Default, Checkbox „Landesgrenzen einzeichnen“)
zeichnet der Render-Kindprozess die Staatsgrenzen (schwarz, ~1 px) und die
österreichischen Bundesländer (schwarz, blasser, ~0,75 px) als geglättete
Haarlinien ins Bild (`overlay.py`, 4-fach überabgetastet), für SEVIRI und FCI (auch Archiv-Renders; der
Cache-Name enthält den Schalter). Daten: Natural Earth 1:10m
`admin_0_boundary_lines_land` + `admin_1_states_provinces_lines` (nur
`STATE_COUNTRIES` = AUT, gemeinfrei), gepackt in
`overlay_data/borders_10m.json.gz` (~370 KB, neu erzeugen mit
`scripts/build_borders.py`). Projiziert wird in dieselbe LAEA wie die
Zielregion - kein pycoast/GSHHS nötig. Keine Küstenlinien.
Umschalten zählt wie ein Kompositwechsel (Frame merkt sich `borders`) und
rendert sofort neu.

### MTG FCI und Rohdaten-Archiv (mtg_fci)

Ein FCI-Slot (alle 10 min, Collection `EO:EUM:DAT:0662`) ist ~1 GB, geteilt
in 40 Streifen („Chunks“, 1 = Süd … 40 = Nord). Geladen werden nur die
Chunks `archive.chunk_min`..`archive.chunk_max` (Default 32–40 = Europa,
~180 MB/Slot, ~25 s) **plus** alle, die eine konfigurierte Region braucht
(`fci_archive.chunks_for_region`: Bounding Box → geos-Projektion →
Zeile im 2-km-Grid; gegen pyproj getestet). Ablage:
`<Speicherort>/_archive/<Collection>/<Slot>/` (`.part` + Rename, `meta.json`
zuletzt), Aufbewahrung `archive.retention_hours` (Default 12 h, 0 = nur
neuester Slot; ~13 GB bei 10 min Takt). Beides in der UI unter
„Speicherort & Historie“.

- Heruntergeladen wird, sobald ein `mtg_fci`-Katalogeintrag **aktiv** ist
  (eigener Download-Job `download-<id>` im 10-min-Takt, ohne Render-Lock - nur I/O),
  unabhängig davon, ob eine Region die Quelle nutzt.
- Gerendert (planmäßig oder bei Bedarf) werden nur die Chunks der Region:
  hält den Kindprozess bei ~410–480 MB. Mit allen 9 Chunks lag die Spitze
  bei ~600–640 MB und wurde neben dem Webserver gelegentlich beendet.
- **Rendern bei Bedarf:** `GET /regions/{r}/archive/{slot}.png?composite=…`
  rendert jede Region (auch mit SEVIRI-Quelle oder neu angelegt) aus jedem
  archivierten Slot, unter dem globalen Render-Lock; das PNG wird im Slot
  unter `renders/` gecacht und verschwindet mit ihm. In der UI: Button
  „🛰 FCI-Archiv“ je Region (Betrachter mit Bildtyp-Auswahl, kein Zeitraffer,
  da jedes Bild ~10–20 s Rendern kostet).
- Reader `fci_l1c_nc` braucht `netCDF4` (Wheel für aarch64 vorhanden) und
  läuft mit dem **synchronen** dask-Scheduler: HDF5 aus dem Wheel ist nicht
  thread-sicher, mit Threads gab es SIGSEGV in ~50 % der Läufe.
- Komposite (UI-Liste je Treiber, `config.FCI_COMPOSITES`): Default
  `natural_color_with_night_cloudtop` (eigene Definition in
  `satpy_config/composites/visir.yaml`, ~11 s / ~410 MB), `natural_color`,
  `hrv_clouds`, `cloudtop`, `colorized_ir_clouds`. **Nicht**: `true_color*`
  (> 6 GB, Zeitlimit) und `airmass` (~1,7 GB). SEVIRI-Namen werden auf ein
  FCI-Gegenstück abgebildet (`FCI_COMPOSITE_ALIASES`), die UI setzt beim
  Quellenwechsel einen passenden Bildtyp.

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

### Download im Aufnahmetakt

Jede Quelle hat einen Takt (`SourcesConfig.cycle_for`): `cycle_minutes`
des Katalogeintrags, sonst bekannter Wert der Collection
(`config.COLLECTION_CYCLES`: Rapid Scan 5, 0°/IODC 15, FCI 10 min) bzw. des
Treibers, sonst `sources.poll_interval_minutes`. Er bestimmt auch die
Frame-Anzahl im Ringpuffer (Historie / Takt).

- Je aktiver Quelle mit Download-Treiber (`msg_seviri` nur, wenn eine Region
  sie nutzt; `mtg_fci` immer, fürs Archiv) läuft ein Takt-Job
  `download-<id>` (alle 20 s, fragt nur bei Fälligkeit). Fällig: Aufnahmeende
  + Takt + beobachtete Lieferverzögerung; noch nicht da → jede Minute, mehr
  als einen Takt überfällig → im Takt, Fehler → nach ≤ 5 min.
- Downloads halten den Render-Lock nicht. Eine **neue** Aufnahme stößt das
  Rendern aller Regionen der Quelle an; gerendert wird aus dem lokal
  liegenden Produkt (kein zweiter Data-Store-Zugriff).
- Platzhalterquellen (`dummy`, `data_tailor`) rendern im festen Intervall.
- Takt, letzte Aufnahme, nächster Download und Fehler je Quelle:
  `GET /api/sources` (`downloads`), in der UI in der Quellen-Tabelle.

### Medien-Cache

Vorschaubilder (`?w=240`), JPEGs für den MJPEG-Stream und GIF/MP4 liegen
unter `<Speicherort>/<Region>/_cache/` (`media.py`) und werden nach jedem
neuen Frame im Job `media-<region>` vorab erzeugt (außerhalb des
Render-Locks). Animationen tragen einen Hash über Frame-Liste und Bildrate
im Namen, Vorschaubilder entfernter Frames räumt der Ringpuffer auf.
Das GIF wird nur bis 60 Frames vorab erzeugt (PIL hält alle Frames im
Speicher), darüber nur auf Anfrage; MP4 liest die Frames einzeln.

## Betriebsregeln

- **Ein Render-Vorgang zur Zeit**, systemweit (nicht nur pro Region) — via
  `threading.Lock` in `RenderScheduler`. Gleichzeitig fällige Läufe warten
  auf den Lock (max. 15 min) und nutzen dasselbe, einmal geladene Produkt.
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
  Neuaufsetzen der Scheduler-Jobs aus (`scheduler.reload()`). Nur neue oder
  geänderte Regionen (z. B. andere Quelle/Komposit) rendern dabei sofort,
  unveränderte warten auf die nächste Aufnahme bzw. behalten ihr Intervall.
- Jeder Frame merkt sich im Index Quelle (`source`) und Komposit. „Keine
  neue Aufnahme“ (`NoNewData`) gilt nur, wenn der neueste Frame dieselbe
  Herkunft hat - nach einem Wechsel wird sofort gerendert, auch wenn die
  neue Quelle eine ältere Aufnahme hat (0° hinkt Rapid Scan ~15 min
  hinterher). Die Puffer-Reihenfolge ist die des Hinzufügens, nicht die der
  Aufnahmezeit.
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
| `GET /` | Konfigurations-UI (ganz unten: API-Link-Sammlung mit Standort-Auswahl) |
| `GET /api/config` | Aktuelle Konfiguration (Secret maskiert) |
| `POST /api/config` | Konfiguration speichern (Merge auf gespeicherte Werte; leeres oder maskiertes Secret = unverändert) |
| `GET /api/status` | Letzter/nächster Lauf, Frame-Anzahl, Alter, Fehler je Region |
| `POST /api/regions/{region}/refresh` | Sofortiger Render-Lauf ("Jetzt aktualisieren") |
| `GET /api/regions/{region}/frames` | Historie: Frames im Puffer (neuester zuerst) mit stabiler URL |
| `GET /api/sources` | Quellen-Katalog, Ergebnis des letzten Abgleichs, Takt/Download-Status je Quelle (`downloads`) |
| `POST /api/sources/sync` | Abgleich mit dem EUMETSAT Data Store jetzt ausführen |
| `POST /api/sources/adopt` | Entdeckte Collection (`{"collection": ...}`) in den Katalog übernehmen |
| `GET /api/storage` | Aktueller Speicherort, Belegung, Kandidaten mit freiem Platz/Warnungen |
| `POST /api/storage` | Speicherort/Historie/Archiv setzen (`frames_dir`, `move_existing`, `history_minutes`, `max_storage_mb`, `archive_retention_hours`, `archive_chunk_min`, `archive_chunk_max`) |
| `GET /api/archive` | FCI-Rohdaten-Archiv: Aufbewahrung, geladene Chunks, Slots/Belegung je Collection, letzter Fehler |
| `GET /api/regions/{region}/archive` | Archivierte FCI-Slots für die Region (`?composite=`), mit Bild-URL und Cache-Status |
| `GET /regions/{region}/archive/{slot}.png` | Region aus einem FCI-Slot rendern (`?composite=`, gecacht) |
| `GET /api/logs` | Log-Einträge (`?after=<id>`, `?format=text`) |
| `GET /live/{region}` | Merkbare Adresse: öffnet den Live-Stream im Browser |
| `GET /healthz` | Liveness |
| `GET /regions/{region}/latest.png` | Neuestes Einzelbild |
| `GET /regions/{region}/frames/{i}.png` | Frame `i` aus dem Puffer (`0` = neuester) |
| `GET /regions/{region}/history/{datei}` | Frame per Dateiname (`?w=240` = JPEG-Vorschaubild, gecacht) |
| `GET /regions/{region}/animation.gif` | Animation der Historie (GIF, gecacht) |
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
schließt), Historie blättern/Zeitraffer, Bild per Auswahlliste wählen,
Zoom im Betrachter (+/−/Reset, Tasten, Ziehen, Desktop + Handy),
Live-Stream im Browser inkl.
Deep-Link `/live/{region}`, Logs ganz unten, Quellen-JSON (eingeklappt,
editierbar, Validierung), Quellen aktivieren, Abgleich + Übernehmen,
Speicherort wechseln inkl. Verschieben, kein horizontales Scrollen am Handy,
Bildtyp/Quelle je Region wechseln (rendert ohne weiteren Klick neu),
Landesgrenzen je Region ein-/ausschalten (Desktop + Handy),
Takt je Quelle anzeigen und per JSON ändern (Desktop + Handy),
Platzhalter-Quellen gekennzeichnet, API-Links folgen dem gewählten Standort
und sind anklickbar.
Selektoren laufen ausschließlich über `data-testid` - Markup und Styling
dürfen sich frei ändern, die IDs müssen erhalten bleiben. Ohne installiertes
Playwright werden die Klicktests lokal übersprungen; in CI erzwingt
`HA_SATELLITE_REQUIRE_UI_TESTS=1`, dass sie laufen.
