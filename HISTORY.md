# HISTORY.md — Chronicle and pitfalls

## 1.0.0 — public release

UI, code and documentation translated to English; deployment-specific
details removed from the documentation. Added semantic versioning
(`__version__` + `pyproject.toml`, `GET /api/version`, UI footer, release
tags `vX.Y.Z` → image tags `X.Y.Z`/`X.Y`/`latest`), a configurable host data
directory (`HOST_DATA_DIR`) and an optional, label-scoped Watchtower
service for automatic updates. See `CHANGELOG.md`.

## Download at the scan cycle, render afterwards, media cache

**Finding:** A global polling interval (15 min) for all sources lost two of
three scans with Rapid Scan (5 min) and every third with FCI (10 min).
SEVIRI also only downloaded during the render run (under the render lock).

**Solution:** Every source has a cycle (`cycle_minutes`, otherwise the known
value of the collection or driver, otherwise `poll_interval_minutes`). For
each source with a download driver a cycle job runs (every 20 s, without
network until it is due). It is due at *end of scan + cycle + delivery
delay*; the delay is the minimum of the recently observed (search time −
end of scan) and thus approaches the real value. If the product is not
there yet, it asks every minute; if the stream stalls for more than one
cycle, only at the cycle; on errors: again after 5 min at the latest. A new
scan triggers rendering of the source's regions; the render uses the
locally stored product (the SEVIRI cache keeps the newest and the previous
one so a running render does not lose its file; FCI the newest archive
slot). SEVIRI only downloads if a region uses the source, FCI always
(archive). Placeholder sources keep rendering at their interval.

**Media cache:** Thumbnails (`?w=`), JPEG for the MJPEG stream and GIF/MP4
were recomputed on every request. Now they live under `<region>/_cache/`
and are pre-generated after every new frame in a separate job. At first
this ran directly in the render job: every run took longer, runs queued up
(click test for composite change turned red).

## Country borders in the image

**Goal:** Orientation in inland regions (Vienna) — for Mallorca the coast
shows where you are. pycoast was rejected: it needs the GSHHS/WDBII dataset
(~150 MB download at runtime or in the image). Instead Natural Earth 1:10m
land borders, rounded to ~100 m and gzip-packed (~370 KB) in the package,
projected with pyproj into the region's LAEA and drawn with PIL. Points
outside 1.5 × radius are discarded (line split) to avoid huge pixel values.

Follow-up after rollout: 1 px yellow with a 3 px black outline looked too
thick (zoomed in the browser), yellow on green landscape had too little
contrast → black hairlines, drawn 4× supersampled and downscaled
(anti-aliased, ~1 px); plus the Austrian federal states (Natural Earth
admin_1, AUT only), fainter.

## MTG FCI with raw-data archive (Europe chunks)

**Goal:** sharper images than SEVIRI (FCI: 1 km visible, 2 km IR) without
~1 GB per 10 min slot, and the ability to choose region/image type only when
viewing. Data Tailor was rejected: it only saves volume but gives no better
quality than the source.

**Spike on a Raspberry Pi 5** (one slot, chunks 32–40 = ~175 MB):
- `fci_l1c_nc` initially failed on the missing `netCDF4` → dependency.
- Satpy fills missing chunks itself (lazily); Vienna rendered from only
  36–37 is pixel-identical to the render from all 9 chunks → the computed
  chunk mapping is correct.
- Memory: all 9 chunks ~600–640 MB peak (killed once — probably the SIGSEGV
  below, not memory; barely better with `num_workers=1` or smaller dask
  chunks); region chunks only ~410–480 MB → always render with just the
  region's chunks.
- Composites: `natural_color` 8 s, `cloudtop` 11 s, custom
  `natural_color_with_night_cloudtop` 11 s / 410 MB; `true_color*` > 6 GB
  and time limit (Rayleigh/ndvi_hybrid_green), `airmass` ~1.7 GB → not
  offered.
- A parallel render outside the render lock (spike next to the running
  service) was killed ("exit code None") — in operation the global lock
  prevents this, and it also applies to on-demand rendering.

**Rollout finding:** FCI renders died non-deterministically without a
result ("exit code None"), although the host had 6 GB free and there was no
OOM kill in the kernel log. With the exit code read correctly: **SIGSEGV**.
Cause: netCDF4/HDF5 from the pip wheels is not thread-safe, `fci_l1c_nc`
reads in parallel with several dask threads. Measurement on the Pi (12
renders each): `threads`/2 workers → 6× SIGSEGV; `synchronous` → 0 crashes,
same runtime (~7.5 s). **Solution:** FCI renders with the synchronous dask
scheduler (`SINGLE_THREADED_READERS`). The "OOM" cases from the spike (9
chunks, parallel render) were very likely the same crash. Additionally
signal/exit code are now reported correctly (only after `join()`), and a
child process that died without a result is restarted once (not on
timeout).

**Decisions:** Archive at the frame storage location under `_archive/`
(large disk), moved as a whole when the storage location changes, not
treated as a region. Download as a separate job without the render lock.
On-demand renders are cached in the slot and deleted with it.

## Source switch without effect, image type selection, API links

**Finding (rollout):** Switching a region from Rapid Scan to 0° (full disk)
showed no new image. The 0° scan (13:27) was older than the newest Rapid
Scan frame (13:40), so the comparison "scan not newer than newest frame"
permanently reported `NoNewData`. **Solution:** frames store source and
composite; comparison only for the same origin, the buffer keeps insertion
order (so the newly rendered image is frame 0). Equal timestamps get a
suffix instead of overwriting each other.

**Misunderstanding "better quality":** 0° is not higher resolution than
Rapid Scan (both SEVIRI), just less frequent (15 instead of 5 min). The real
quality improvement is the composite (HRV sharpening, see below), which was
not configurable in the UI. Now: dropdown "Image type" per region, and
sources with placeholder drivers (`data_tailor`, `mtg_fci` at the time) are
marked as "placeholder".

**Side finding:** Every save (also interval, source checkboxes) set all
region jobs to "now" — several render runs in a row and APScheduler warnings
"maximum number of running instances". Unchanged regions now keep their
cycle.

**API links:** At the bottom of the UI; the region selection changes the
region links; POST endpoints are only displayed.

## HRV sharpening (higher resolution by day)

**Reason:** The images were very pixelated. SEVIRI colour channels have
~3 km (over Central Europe rather 4–6 km), but the regions ~0.75 km/px
(600 km on 800 px) — every satellite pixel became a 5–8 px block.

**Implementation:** New default composite `natural_color_hrv_with_night_ir`
(`src/ha_satellite/satpy_config/composites/seviri.yaml`, included via
`satpy.config config_path`). By day the true colour channels are sharpened
with the HRV channel (~1 km, in the same `.nat` file, no extra download),
at night `cloudtop` as before (IR, ~3 km). Runtime on a Pi 5 ~8–9 s, peak
RSS ~400–450 MB per region.

**Pitfalls:**

- **Different grids:** HRV (RSS: 5568×4176) and VIS/IR (3712×1392) have
  their own areas. Satpy only creates such composites during
  `Scene.resample`. The crop window is therefore computed per dataset from
  its own area and written back into the *original* scene
  (`scene._datasets[...]`, so wishlist/dependency tree are kept); then
  `resample` creates the composite. Loading uses
  `scene.load(..., generate=False)`, i.e. single-grid composites and
  modifiers (`sunz_corrected`) are also only created on the small target
  grid — peak RSS dropped for all composites (e.g.
  `natural_color_raw_with_night_ir` RSS 570 → 380 MB, on 0° 880 → 430 MB).
- **Full disk (0°, catalogue entry `msg_seviri_0deg`):** HRV comes as two
  stacked windows (`StackedAreaDefinition`), which breaks cropping and
  `sunz_corrected` (dask: "Shapes do not align"). The reader therefore runs
  with `fill_disk=True` (HRV as one 11136×11136 grid, filled lazily). Also
  `source_window` now only evaluates points where row *and* column are
  valid (previously compressed independently — in the southern HRV window
  only the columns were valid).
- **`RatioSharpenedRGB` (Satpy):** replaces one channel with HRV and scales
  the others by HRV/channel — over dark sea (HRV contains blue scattered
  light, VIS008 almost 0) a clear magenta cast appeared.
- **Classic pansharpening** (HRV / HRV averaged to 3 km): the 3×3 mean does
  not align with the VIS grid, the blocks stayed visible.
- **Solution:** custom `HrvLuminanceSharpenedRGB`
  (`sources/hrv_composite.py`): all three channels × `HRV / mean(VIS006,
  VIS008)`. The denominator lies exactly on the colour grid, hue is
  preserved; factor capped at 3 (1.5 left clouds over sea blurry).
- **Existing installations** keep the composite stored in
  `/data/config.yaml` — to switch, set `composite:` per region to
  `natural_color_hrv_with_night_ir` there.

## Phase 2 — MSG SEVIRI with Satpy (real images)

**Implementation:** `msg_seviri` loads the newest product from
`EO:EUM:DAT:MSG:MSG15-RSS` (Rapid Scan, 5 min) and renders it with Satpy in
a child process. Details: AGENTS.md, section Phase model.

**Pitfalls:**

- **Product size:** An RSS product is ~97 MB (`.nat`), not the often quoted
  ~54 MB. Therefore each product is loaded exactly once and rendered for all
  regions from `/data/cache/` (only the newest is kept).
- **pyresample `reduce_data` / `Scene.crop` with the RSS area:** The Rapid
  Scan area (3712×1392, northern strip only) is stored with a mirrored x
  extent. pyresample's pre-crop computes wrong windows for it — Vienna was
  99 % black (window 26×4 pixels), Mallorca correct by chance.
  `reduce_data=False` is correct but processes the whole scene (~1.3 GB
  RAM). Solution: own window via `get_array_indices_from_lonlat` of the
  target region's edges (`satpy_render.source_window`), slice the scene
  directly, then nearest neighbour with `reduce_data=False`. Regression test
  with the real geometry in `tests/test_sources.py`.
- **`natural_color_with_night_ir`** downloads the NASA BlackMarble map via
  pooch at runtime — the stored hash does not match, the composite fails.
  The default was therefore `natural_color_raw_with_night_ir`.
- **Scheduler lock:** Both regions start at the same time; the previous
  non-blocking lock skipped the second region *every time* (it never got an
  image). Now it waits (timeout 15 min) and uses the already loaded product.
- **conda-forge/aarch64:** `satpy=0.60` cannot be solved on linux-aarch64
  (no current `trollimage`). The Satpy chain therefore comes from PyPI
  wheels (`--only-binary`), conda only for Python.

## Phase 1 — skeleton (config + web server + dummy frame)

**Decision:** Before connecting the EUMETSAT/Satpy chain, a fully working
skeleton exists: configuration (YAML + env override), FastAPI web server
with all required endpoints, ring buffer logic and scheduler. All sources
(`msg_seviri`, `data_tailor`, `mtg_fci`) deliberately only deliver a
generated placeholder image ("dummy frame") with timestamp, region and
bounding box in this phase.

**Reason:** This allows verifying the critical ARM64 build (see below) and
the whole infrastructure (Docker Compose, GHCR push, Home Assistant
integration via MJPEG) early and with low risk, without Satpy, pyresample,
pykdtree and pyproj slowing down the build and test cycle.

**Open points to be clarified for phase 2** (deliberately not assumed but
documented here):

- Exact eumdac search pattern per source (product IDs, collections) for
  `msg_seviri`, `data_tailor`, `mtg_fci`.
- Data Tailor chain configuration (target projection, format) per region.
- Behaviour during Data Store maintenance windows/rate limits (backoff).
- Whether `natural_color` and other RGB composites suffice as Satpy standard
  composites or custom composite definitions are needed.

## ARM64 build: micromamba instead of plain `python:slim` + pip

**Pitfall:** `pyresample`, `pykdtree` and `pyproj` (Satpy dependencies)
build very slowly from the PyPI sdist on ARM64 or need extra native
libraries (PROJ). A naive `FROM python:3.12-slim` + `pip install satpy`
would have led to very long or unreliable builds on a Pi 5 (or in QEMU
emulation in CI).

**Decision:** `mambaorg/micromamba` as base image, packages from the
`conda-forge` channel, which provides ready `linux-aarch64` builds for
exactly these packages. Details and alternatives in `AGENTS.md`.

**Consequence for phase 1:** The Dockerfile was switched to micromamba
already, although only pure Python packages (FastAPI, Pillow, …) were
needed — so the move to Satpy in phase 2 required no base image change and
the ARM64 build worked from the start.

## Ring buffer index

**Pitfall:** Without a persistent index (frame order) it is no longer
reliably possible after a container restart to tell which frame is the
newest, especially if file names are not strictly lexically sortable.
Solution: `_index.json` per region directory, reconciled on start with the
files actually present (missing files removed, orphan files without an
index entry deleted).

## Single render lock

**Pitfall:** With several active regions on the same polling interval,
APScheduler jobs fire almost simultaneously. Without a lock, renders would
run in parallel and compete for CPU/RAM with other workloads on a small
host. A process-wide `threading.Lock` in `RenderScheduler` ensures only one
render is active at a time (later changed from skipping to waiting, see
phase 2).

## Bind mount ownership vs. non-root container

**Pitfall:** The Dockerfile chowns `/data` at build time to the container
user (`mambauser`, high "non-root-typical" UID 57439). But when `/data` is
mounted from the host as a volume/bind mount (as in `docker-compose.yml`),
the host ownership of the mounted directory completely overrides the
ownership set in the image — the build-time `chown` has no effect. Result:
`PermissionError` on the first render attempt as soon as a fresh,
root-owned data directory is mounted (reproduced and verified locally with
`docker build` + `docker run -v ... ha_satellite:test`).

**Solution:** `docker/entrypoint.sh` starts the container as `root` by
default (no `USER` directive in the Dockerfile anymore), aligns the actual
ownership of the mounted volume on start via
`chown -R mambauser:mambauser /data` and then switches to the unprivileged
user with `setpriv --reuid=mambauser --regid=mambauser --init-groups`
before starting `uvicorn` (verified with `docker top`: the main process
runs as UID 57439, not root). `gosu`/`su-exec` are not preinstalled in the
micromamba image, `setpriv` (util-linux) is.

## Credentials could not be saved (rollout finding)

At the first rollout the web UI was read-only — there was neither a form nor
a save button. Behind it were two more bugs that would have made setting
them via the API useless too:

- **Empty env variables as override:** `docker-compose.yml` passes
  `EUMETSAT_CONSUMER_KEY=${EUMETSAT_CONSUMER_KEY:-}`, so inside the
  container the variable is *set but empty*. The code only checked for
  `None` and thus overwrote every stored input with `""`. Empty values no
  longer count as override (`config.env_overrides()`).
- **Env values ended up in `config.yaml`:** `POST /api/config` merged onto
  the *effective* configuration (incl. env override) and wrote the result
  back. `ConfigStore` therefore now keeps `stored()` (file content) and
  `get()` (effective) separate; changes always build on `stored()`.
- **Masked secret as echo:** If a client sends back the masked secret (or an
  empty field), the stored secret is kept.

Since then browser click tests (`tests/test_ui.py`, Playwright) secure the
core UI features. Selectors use `data-testid` exclusively so layout changes
do not break the tests — these IDs must be kept when reworking the UI. In
CI `HA_SATELLITE_REQUIRE_UI_TESTS=1` enforces that the click tests run
instead of being silently skipped.

## Web UI expansion: sources, viewer, live, logs, storage location

- **Source catalogue instead of `sources.active`:** Which sources exist is
  now a list in `sources.catalog` (editable in the UI as a table and as
  collapsible JSON). Regions reference a catalogue `id`; validation for it
  sits at the `AppConfig` level (previously: fixed list in `RegionConfig`).
  Old configurations are migrated on load.
- **Source sync:** The browse/search endpoints of the EUMETSAT Data Store
  work without a token. The sync deliberately stores its result in
  `/data/source_sync.json` instead of `config.yaml`, so the user-edited
  catalogue is not mixed with runtime data. Tests run against a local fake
  server; the test server points to a closed port by default so the real
  API is never queried.
- **Fullscreen viewer instead of popup:** A `position: fixed; inset: 0`
  overlay with `100dvh` (phone browser bars) and `object-fit: contain`.
  Opening adds a history entry so the back button on phones closes the
  viewer instead of leaving the page. The MJPEG stream is stopped on close
  by removing `src`, otherwise it would keep running in the background.
- **Pitfall `[hidden]`:** A `display` rule for `button` overrides the
  `hidden` attribute; the click tests uncovered this. Globally
  `[hidden] { display: none !important; }`.
- **Pitfall phone width:** Long paths (e.g. in the storage section) in
  flex/grid elements forced horizontal scrolling. Solution:
  `overflow-wrap: anywhere` and `minmax(0, 1fr)`; a click test checks
  `scrollWidth <= 390` in the phone viewport.
- **Start collision of regions:** All region jobs started simultaneously;
  due to the global render lock all but the first were skipped until the
  next interval (15 min) — immediately visible in the new live logs. Solved
  by waiting for the lock from phase 2 (instead of skipping); the interim
  60 s offset was dropped in the merge.
- **Storage location:** Compose mounts `/mnt`; the UI offers its
  subdirectories and warns based on `st_dev` if a path is on the same disk
  as `/data` or only in the container filesystem. So the service may write
  to host disks, the entrypoint supports `PUID`/`PGID`
  (`usermod`/`groupmod` on `mambauser`).
- **APScheduler logs** are set to WARNING, otherwise they would log every
  job run at INFO and push the render messages out of the UI.
