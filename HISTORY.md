# HISTORY.md — Chronik und Fallstricke

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
