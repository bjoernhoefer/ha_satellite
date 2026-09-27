# HISTORY.md — Chronik und Fallstricke

## Landesgrenzen im Bild

**Ziel:** Orientierung in Binnenregionen (Wien) - bei Mallorca zeigt die
Küste, wo man ist. pycoast wurde verworfen: braucht den GSHHS/WDBII-Datensatz
(~150 MB Download zur Laufzeit oder im Image). Stattdessen Natural Earth
1:10m Landgrenzen, auf ~100 m gerundet und gzip-gepackt (~370 KB) im Paket,
mit pyproj in die LAEA der Region projiziert und per PIL gezeichnet.
Punkte außerhalb von 1,5 × Radius werden verworfen (Linie aufgetrennt),
damit keine riesigen Pixelwerte entstehen.

Nachbesserung nach Rollout: 1-px-Gelb mit 3-px-Schwarzrand wirkte (im
Browser vergrößert) zu dick, Gelb auf grüner Landschaft zu kontrastarm →
schwarze Haarlinien, 4-fach überabgetastet gezeichnet und verkleinert
(geglättet, ~1 px); dazu die österreichischen Bundesländer (Natural Earth
admin_1, nur AUT) blasser.

## MTG FCI mit Rohdaten-Archiv (Europa-Chunks)

**Ziel:** schärfere Bilder als SEVIRI (FCI: 1 km sichtbar, 2 km IR) ohne
~1 GB je 10-min-Slot, und die Möglichkeit, Region/Bildtyp erst beim
Anschauen zu wählen. Data Tailor wurde verworfen: spart nur Volumen, liefert
aber keine bessere Qualität als die Quelle.

**Spike auf dem Pi 5** (ein Slot, Chunks 32–40 = ~175 MB):
- `fci_l1c_nc` scheiterte zunächst am fehlenden `netCDF4` → Dependency.
- Satpy füllt fehlende Chunks selbst auf (lazy); Wien aus nur 36–37 gerendert
  ist pixelgleich zum Render aus allen 9 Chunks → die berechnete Chunk-Zuordnung
  stimmt.
- Speicher: alle 9 Chunks ~600–640 MB Spitze (einmal beendet - vermutlich
  der SIGSEGV unten, nicht Speicher; trotz
  `num_workers=1` bzw. kleineren dask-Chunks kaum besser); nur Regions-Chunks
  ~410–480 MB → Rendern immer nur mit den Chunks der Region.
- Komposite: `natural_color` 8 s, `cloudtop` 11 s, eigenes
  `natural_color_with_night_cloudtop` 11 s / 410 MB; `true_color*` > 6 GB
  und Zeitlimit (Rayleigh/ndvi_hybrid_green), `airmass` ~1,7 GB → nicht
  anbieten.
- Ein Parallel-Render außerhalb des Render-Locks (Spike neben dem laufenden
  Dienst) wurde beendet („Exit-Code None“) - im Betrieb verhindert das der
  globale Lock, der auch für das Rendern bei Bedarf gilt.

**Rollout-Befund:** FCI-Renders starben nichtdeterministisch ohne Ergebnis
(„Exit-Code None“), obwohl der Host 6 GB frei hatte und kein OOM-Kill im
Kernel-Log stand. Mit korrekt gelesenem Exit-Code: **SIGSEGV**. Ursache:
netCDF4/HDF5 aus den pip-Wheels ist nicht thread-sicher, `fci_l1c_nc` liest
mit mehreren dask-Threads parallel. Messung auf dem Pi (je 12 Renders):
`threads`/2 Worker → 6× SIGSEGV; `synchronous` → 0 Abstürze, gleiche
Laufzeit (~7,5 s). **Lösung:** FCI rendert mit dem synchronen dask-Scheduler
(`SINGLE_THREADED_READERS`). Die „OOM“-Fälle aus dem Spike (9 Chunks,
Parallel-Render) waren sehr wahrscheinlich derselbe Absturz. Zusätzlich
wird Signal/Exit-Code jetzt korrekt gemeldet (erst nach `join()`), und ein
ohne Ergebnis gestorbener Kindprozess wird einmal neu gestartet (nicht bei
Zeitüberschreitung).

**Entscheidungen:** Archiv am Bilder-Speicherort unter `_archive/` (große
Platte), beim Speicherort-Wechsel als Ganzes mit umgezogen, nicht als Region
behandelt. Download als eigener Job ohne Render-Lock. Bei Bedarf gerenderte
Bilder werden im Slot gecacht und mit ihm gelöscht.

## Quellenwechsel ohne Wirkung, Bildtyp-Auswahl, API-Links

**Befund (Rollout):** Wechsel einer Region von Rapid Scan auf 0° (Full Disk)
zeigte kein neues Bild. Die 0°-Aufnahme (13:27) war älter als der neueste
Rapid-Scan-Frame (13:40), der Vergleich "Aufnahme nicht neuer als neuester
Frame" meldete daher dauerhaft `NoNewData`. **Lösung:** Frames speichern
Quelle und Komposit; verglichen wird nur bei gleicher Herkunft, der Puffer
behält die Reihenfolge des Hinzufügens (damit ist das neu gerenderte Bild
Frame 0). Gleiche Zeitstempel bekommen einen Suffix statt sich zu
überschreiben.

**Missverständnis „bessere Qualität“:** 0° ist nicht höher aufgelöst als
Rapid Scan (beides SEVIRI), nur seltener (15 statt 5 min). Die echte
Qualitätsverbesserung ist das Komposit (HRV-Schärfung, siehe unten); das war
in der UI nicht einstellbar. Jetzt: Auswahlliste „Bildtyp“ je Region, und
Quellen mit Platzhalter-Treibern (`data_tailor`, `mtg_fci`) sind als
„Platzhalter“ gekennzeichnet.

**Nebenbefund:** Jedes Speichern (auch Intervall, Quellen-Häkchen) setzte alle
Region-Jobs auf „sofort“ - mehrere Render-Läufe hintereinander und
APScheduler-Warnungen „maximum number of running instances“. Jetzt behalten
unveränderte Regionen ihren Takt.

**API-Links:** Ganz unten in der UI, Standort-Auswahl ändert die
Regions-Links; POST-Endpunkte werden nur angezeigt.

## HRV-Schärfung (höhere Auflösung tagsüber)

**Anlass:** Die Bilder waren sehr pixelig. SEVIRI-Farbkanäle haben ~3 km
(über Mitteleuropa eher 4–6 km), die Regionen aber ~0,75 km/px (600 km auf
800 px) - jedes Satellitenpixel wurde zu einem 5–8 px großen Block.

**Umsetzung:** Neues Default-Komposit `natural_color_hrv_with_night_ir`
(`src/ha_satellite/satpy_config/composites/seviri.yaml`, per
`satpy.config config_path` eingebunden). Tagsüber werden die
Echtfarbkanäle mit dem HRV-Kanal (~1 km, in derselben `.nat`-Datei, kein
Mehr-Download) geschärft, nachts wie bisher `cloudtop` (IR, ~3 km).
Laufzeit auf dem Pi 5 ~8–9 s, Spitzen-RSS ~400–450 MB pro Region.

**Fallstricke:**

- **Unterschiedliche Grids:** HRV (RSS: 5568×4176) und VIS/IR (3712×1392)
  haben eigene Areas. Solche Komposite erzeugt Satpy erst beim
  `Scene.resample`. Das Crop-Fenster wird deshalb je Datensatz aus dessen
  eigener Area berechnet und in die *ursprüngliche* Scene zurückgeschrieben
  (`scene._datasets[...]`, damit Wishlist/Abhängigkeitsbaum erhalten
  bleiben); danach erzeugt `resample` das Komposit. Geladen wird mit
  `scene.load(..., generate=False)`, d. h. auch Ein-Raster-Komposite und
  Modifier (`sunz_corrected`) entstehen erst auf dem kleinen Zielraster -
  Spitzen-RSS sank dadurch für alle Komposite (z. B.
  `natural_color_raw_with_night_ir` auf RSS 570 → 380 MB, auf 0° 880 →
  430 MB).
- **Full Disk (0°, Katalogeintrag `msg_seviri_0deg`):** HRV liegt dort als
  zwei gestapelte Fenster vor (`StackedAreaDefinition`), daran scheitern
  Zuschnitt und `sunz_corrected` (dask: "Shapes do not align"). Der Reader
  läuft deshalb mit `fill_disk=True` (HRV als ein 11136×11136-Raster, lazy
  aufgefüllt). Außerdem wertet `source_window` nur noch Punkte aus, bei
  denen Zeile *und* Spalte gültig sind (vorher unabhängig komprimiert -
  im südlichen HRV-Fenster waren nur die Spalten gültig).
- **`RatioSharpenedRGB` (Satpy):** ersetzt einen Kanal durch HRV und
  skaliert die anderen mit HRV/Kanal - über dunklem Meer (HRV enthält
  blaues Streulicht, VIS008 fast 0) entstand ein deutlicher Magentastich.
- **Klassisches Pansharpening** (HRV / auf 3 km gemitteltes HRV): Das
  3×3-Mittel liegt nicht deckungsgleich auf dem VIS-Raster, die Blöcke
  blieben sichtbar.
- **Lösung:** eigener `HrvLuminanceSharpenedRGB`
  (`sources/hrv_composite.py`): alle drei Kanäle × `HRV / mean(VIS006,
  VIS008)`. Der Nenner liegt exakt im Farbraster, Farbton bleibt erhalten;
  Faktor auf 3 gekappt (1,5 ließ Wolken über Meer unscharf).
- **Bestehende Installationen** behalten das in `/data/config.yaml`
  gespeicherte Komposit - zum Umstellen dort `composite:` je Region auf
  `natural_color_hrv_with_night_ir` setzen.

## Phase 2 — MSG SEVIRI mit Satpy (echte Bilder)

**Umsetzung:** `msg_seviri` lädt das neueste Produkt aus
`EO:EUM:DAT:MSG:MSG15-RSS` (Rapid Scan, 5 min) und rendert es mit Satpy in
einem Kindprozess. Details: AGENTS.md, Abschnitt Phasenmodell.

**Fallstricke:**

- **Produktgröße:** Ein RSS-Produkt ist ~97 MB (`.nat`), nicht die oft
  genannten ~54 MB. Deshalb wird pro Produkt genau einmal geladen und für
  alle Regionen aus `/data/cache/` gerendert (nur das neueste bleibt liegen).
- **pyresample `reduce_data` / `Scene.crop` bei der RSS-Area:** Die
  Rapid-Scan-Area (3712×1392, nur nördlicher Streifen) ist mit
  gespiegeltem x-Extent gespeichert. pyresamples Vorab-Zuschnitt berechnet
  dafür falsche Fenster — Wien war zu 99 % schwarz (Fenster 26×4 Pixel),
  Mallorca zufällig korrekt. `reduce_data=False` ist korrekt, verarbeitet
  aber die ganze Szene (~1,3 GB RAM). Lösung: eigenes Fenster über
  `get_array_indices_from_lonlat` der Zielregion-Ränder
  (`satpy_render.source_window`), Szene direkt slicen, dann Nearest-
  Neighbour mit `reduce_data=False`. Regressionstest mit der echten
  Geometrie in `tests/test_sources.py`.
- **`natural_color_with_night_ir`** lädt zur Laufzeit die NASA-BlackMarble-
  Karte per pooch — der hinterlegte Hash passt nicht, das Komposit
  scheitert. Default ist daher `natural_color_raw_with_night_ir`.
- **Scheduler-Lock:** Beide Regionen starten zur selben Zeit; der bisherige
  nicht-blockierende Lock ließ die zweite Region *jedes Mal* aus (sie
  bekam nie ein Bild). Jetzt wartet sie (Timeout 15 min) und nutzt das
  bereits geladene Produkt.
- **conda-forge/aarch64:** `satpy=0.60` ist auf linux-aarch64 nicht lösbar
  (keine aktuelle `trollimage`). Die Satpy-Kette kommt deshalb aus
  PyPI-Wheels (`--only-binary`), conda nur für Python.

## Phase 1 — Grundgerüst (Config + Web-Server + Dummy-Frame)

**Entscheidung:** Bevor die EUMETSAT/Satpy-Kette angebunden wird, steht ein
vollständig lauffähiges Grundgerüst: Konfiguration (YAML + Env-Override),
FastAPI-Web-Server mit allen im Lastenheft geforderten Endpunkten,
Ringpuffer-Logik und Scheduler. Alle Quellen (`msg_seviri`, `data_tailor`,
`mtg_fci`) liefern in dieser Phase bewusst nur ein generiertes
Platzhalterbild ("Dummy-Frame") mit Zeitstempel, Region und Bounding-Box.

**Begründung:** So lässt sich der kritische ARM64-Build (siehe unten) sowie
die gesamte Infrastruktur (Docker Compose, GHCR-Push, Home-Assistant-
Integration über MJPEG) früh und risikoarm verifizieren, ohne dass Satpy,
pyresample, pykdtree und pyproj den Build- und Testzyklus verlangsamen.

**Offene Punkte, die für Phase 2 geklärt werden müssen** (bewusst noch nicht
angenommen, sondern hier dokumentiert):

- Genaues eumdac-Suchmuster je Quelle (Produkt-IDs, Collections) für
  `msg_seviri`, `data_tailor`, `mtg_fci`.
- Data-Tailor-Chain-Konfiguration (Zielprojektion, Format) je Region.
- Verhalten bei Data-Store-Wartungsfenstern/Rate-Limits (Backoff-Strategie).
- Ob `natural_color` u. a. RGB-Komposite als Satpy-Standardkomposite
  ausreichen oder eigene Komposit-Definitionen nötig sind.

## ARM64-Build: micromamba statt reinem `python:slim` + pip

**Fallstrick:** `pyresample`, `pykdtree` und `pyproj` (Satpy-Abhängigkeiten)
bauen aus dem PyPI-Sdist heraus auf ARM64 sehr langsam bzw. benötigen
zusätzliche native Bibliotheken (PROJ). Ein naiver
`FROM python:3.12-slim` + `pip install satpy` hätte auf dem Pi 5 (oder in
QEMU-Emulation im CI) zu sehr langen bzw. unzuverlässigen Builds geführt.

**Entscheidung:** `mambaorg/micromamba` als Basis-Image, Pakete aus dem
`conda-forge`-Kanal, der fertige `linux-aarch64`-Builds für genau diese
Pakete bereitstellt. Details und Alternativen-Abwägung in `AGENTS.md`.

**Konsequenz für Phase 1:** Das Dockerfile ist schon jetzt auf micromamba
umgestellt, obwohl aktuell nur reine Python-Pakete (FastAPI, Pillow, …)
gebraucht werden - damit der Umstieg auf Satpy in Phase 2 keinen Wechsel
des Basis-Images mehr erfordert und der ARM64-Build von Anfang an
funktioniert (statt erst kurz vor Phase 2 überrascht zu werden).

## Ringpuffer-Index

**Fallstrick:** Ohne einen persistenten Index (Reihenfolge der Frames) lässt
sich nach einem Container-Neustart nicht mehr zuverlässig sagen, welcher
Frame der neueste ist, insbesondere wenn Dateinamen nicht streng lexikalisch
sortierbar sind. Lösung: `_index.json` je Region-Verzeichnis, das beim Start
gegen die tatsächlich vorhandenen Dateien abgeglichen wird (fehlende Dateien
raus, Waisen-Dateien ohne Index-Eintrag werden gelöscht).

## Single-Render-Lock

**Fallstrick:** Bei mehreren aktiven Regionen mit demselben Abrufintervall
lösen APScheduler-Jobs nahezu gleichzeitig aus. Ohne Sperre würden Rendering-
Vorgänge parallel laufen und sich mit `grafana` um CPU/RAM auf dem Pi 5
prügeln. Ein prozessweiter `threading.Lock` in `RenderScheduler` sorgt dafür,
dass immer nur ein Render-Vorgang aktiv ist; kollidierende Jobs werden für
diesen Zyklus übersprungen und beim nächsten Intervall erneut versucht.

## Bind-Mount-Ownership vs. nicht-root-Container

**Fallstrick:** Das Dockerfile chownt `/data` beim Build auf den
Container-Nutzer (`mambauser`, hochgezählte, "nicht-root-typische" UID
57439). Wird `/data` jedoch als Docker-Volume/Bind-Mount vom Host
eingehängt (wie in `docker-compose.yml` für `./data:/data`), überschreibt
die Host-Ownership des gemounteten Verzeichnisses die im Image gesetzte
Ownership vollständig - der Build-Zeit-`chown` läuft dadurch ins Leere.
Ergebnis: `PermissionError` beim ersten Render-Versuch, sobald ein frisches,
root-eigenes `./data`-Verzeichnis gemountet wird (mit `docker build` +
`docker run -v ... ha_satellite:test` lokal reproduziert und verifiziert).

**Lösung:** `docker/entrypoint.sh` startet den Container standardmäßig als
`root` (kein `USER`-Directive mehr im Dockerfile), gleicht beim Start via
`chown -R mambauser:mambauser /data` die tatsächliche Ownership des
gemounteten Volumes an und wechselt danach mit `setpriv --reuid=mambauser
--regid=mambauser --init-groups` in den unprivilegierten Nutzer, bevor
`uvicorn` gestartet wird (verifiziert per `docker top`: Hauptprozess läuft
als UID 57439, nicht als root). `gosu`/`su-exec` sind im micromamba-Image
nicht vorinstalliert, `setpriv` (util-linux) dagegen schon.

## Zugangsdaten ließen sich nicht speichern (Rollout-Befund)

Beim ersten Rollout auf `nzbpi` war die Web-UI rein lesend - es gab weder
Formular noch Speichern-Button. Dahinter steckten zwei weitere Fehler, die
auch das Setzen per API unbrauchbar gemacht hätten:

- **Leere Env-Variablen als Override:** `docker-compose.yml` reicht
  `EUMETSAT_CONSUMER_KEY=${EUMETSAT_CONSUMER_KEY:-}` durch, im Container ist
  die Variable also *gesetzt, aber leer*. Der Code prüfte nur auf `None` und
  überschrieb damit jede gespeicherte Eingabe mit `""`. Leere Werte zählen
  jetzt nicht mehr als Override (`config.env_overrides()`).
- **Env-Werte landeten in `config.yaml`:** `POST /api/config` mergte auf die
  *effektive* Konfiguration (inkl. Env-Override) und schrieb das Ergebnis
  zurück. `ConfigStore` hält deshalb jetzt getrennt `stored()` (Dateiinhalt)
  und `get()` (effektiv); Änderungen setzen immer auf `stored()` auf.
- **Maskiertes Secret als Echo:** Schickt ein Client das maskierte Secret
  (oder ein leeres Feld) zurück, bleibt das gespeicherte Secret erhalten.

Seitdem sichern Browser-Klicktests (`tests/test_ui.py`, Playwright) die
Grundfunktionen der UI ab. Selektoren laufen ausschließlich über
`data-testid`, damit Layout-Änderungen die Tests nicht brechen - beim
Umbau der UI müssen diese IDs erhalten bleiben. In CI erzwingt
`HA_SATELLITE_REQUIRE_UI_TESTS=1`, dass die Klicktests laufen statt still
übersprungen zu werden.

## Web-UI-Ausbau: Quellen, Betrachter, Live, Logs, Speicherort

- **Quellen-Katalog statt `sources.active`:** Welche Quellen es gibt, steht
  jetzt als Liste in `sources.catalog` (in der UI als Tabelle und
  einklappbares JSON editierbar). Regionen referenzieren eine Katalog-`id`,
  die Validierung dafür sitzt auf `AppConfig`-Ebene (vorher: feste Liste in
  `RegionConfig`). Alte Konfigurationen werden beim Laden migriert.
- **Quellen-Abgleich:** Die Browse-/Search-Endpunkte des EUMETSAT Data Store
  sind ohne Token nutzbar. Der Abgleich speichert sein Ergebnis bewusst in
  `/data/source_sync.json` statt in `config.yaml`, damit der vom Nutzer
  editierte Katalog nicht mit Laufzeitdaten vermischt wird. Tests laufen gegen
  einen lokalen Fake-Server; der Test-Server zeigt per Default auf einen
  geschlossenen Port, damit nie die echte API angefragt wird.
- **Vollbild-Betrachter statt Popup:** Ein `position: fixed; inset: 0`-Overlay
  mit `100dvh` (Handy-Browserleisten) und `object-fit: contain`. Öffnen legt
  einen History-Eintrag an, damit die Zurück-Taste am Handy den Betrachter
  schließt statt die Seite zu verlassen. Der MJPEG-Stream wird beim Schließen
  durch Entfernen von `src` beendet, sonst liefe er im Hintergrund weiter.
- **Fallstrick `[hidden]`:** Eine `display`-Regel für `button` überschreibt das
  `hidden`-Attribut; die Klicktests haben das aufgedeckt. Global
  `[hidden] { display: none !important; }`.
- **Fallstrick Handy-Breite:** Lange Pfade (z. B. im Speicherort-Bereich) in
  Flex-/Grid-Elementen erzwangen horizontales Scrollen. Lösung:
  `overflow-wrap: anywhere` und `minmax(0, 1fr)`; ein Klicktest prüft
  `scrollWidth <= 390` im Handy-Viewport.
- **Startkollision der Regionen:** Alle Region-Jobs starteten gleichzeitig,
  wegen des globalen Render-Locks wurde jede außer der ersten bis zum
  nächsten Intervall (15 min) übersprungen - in den neuen Live-Logs sofort
  sichtbar. Gelöst durch das Warten auf den Lock aus Phase 2 (statt
  Überspringen); der zwischenzeitliche 60-s-Versatz entfiel beim Merge.
- **Speicherort:** Compose hängt `/mnt` ein, die UI bietet dessen
  Unterordner an und warnt anhand von `st_dev`, wenn ein Pfad auf demselben
  Datenträger wie `/data` oder nur im Container-Dateisystem liegt. Damit der
  Dienst auf Host-Datenträger schreiben darf, unterstützt das Entrypoint
  `PUID`/`PGID` (`usermod`/`groupmod` auf `mambauser`).
- **APScheduler-Logs** stehen auf WARNING, sonst würden sie jeden Job-Lauf auf
  INFO protokollieren und die Render-Meldungen in der UI verdrängen.
