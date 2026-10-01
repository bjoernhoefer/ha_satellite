# Changelog

All notable changes to this project are documented in this file.
The project follows [Semantic Versioning](https://semver.org/).

## [1.2.0] — 2026-10-01

### Added

- **Locations on a map of Europe** ("location_change"): up to 4 locations
  can be added, moved and removed in the web UI. "+ Add location" and
  "📍 Change location" open a zoomable map of Europe (wheel/pinch, drag,
  +/− buttons) with land, sea and country borders in the same style as the
  image overlay (incl. Austrian states); click/tap marks the centre, the
  radius is set with a slider, latitude/longitude can also be typed in
  (plain WGS84, no Meteosat pixel numbers). Other locations are shown on the
  map. Deep link `#location=<name>` / `#location=new`.
- On-demand satellite background for the map ("🛰 Load current satellite
  map"): rendered from the latest MSG SEVIRI scan (downloaded if needed)
  with country borders and cached until it is rendered again.
- API: `POST /api/regions`, `PUT /api/regions/{region}`,
  `DELETE /api/regions/{region}`, `GET /api/location-map`,
  `GET /location-map/outline.png`, `GET /location-map/satellite.jpg`,
  `POST /api/location-map/satellite`.
- Natural Earth 1:50m land polygons for Europe
  (`overlay_data/europe_land_50m.json.gz`, `scripts/build_europe_land.py`).
- Unit, API, rendering and click tests (desktop and phone) for locations
  and the map.

### Changed

- At most 4 locations: saving a configuration that adds a fifth region is
  rejected (existing larger configurations remain usable); duplicate region
  names are rejected.
- Moving or removing a location deletes its image history, so the history
  never mixes cut-outs and the new cut-out renders immediately.

## [1.1.0] — 2026-10-01

### Added

- **Download all sources**: a new switch "Download all sources" (under
  "Storage location & history") downloads every enabled source at its own
  cycle (MSG SEVIRI 0° every 15 min, Rapid Scan every 5 min, MTG FCI every
  10 min) and renders **every region in every image type** of that source
  right after a new scan. The images are stored as PNG and JPEG under
  `_renders/<region>/<source>/<image type>/<scan>.png` at the frame storage
  location; cleanup by age (`render_retention_hours`, default 24 h) and by
  a storage limit (`render_max_storage_mb`, default 2000 MB).
- Button "Archive all now" for an immediate run over all enabled sources
  (`POST /api/archive/render`).
- New unit, API and click tests (desktop and phone) for the archive.

### Changed

- The per-region button "🛰 FCI archive" is now "🗄 Archive" and opens the
  generalized archive viewer: source and image type can be chosen, images
  are already rendered, so browsing is as fast as the normal viewer and
  time-lapse works here too. Format selection (JPEG/PNG) is available as
  well. Deep links carry `#archive=<region>&s=<source>&c=<image type>&f=<format>`.
- `GET /api/regions/{region}/archive` now returns `sources`, `composites`
  and `images` (the old key `slots` remains as an alias); images are served
  from `GET /regions/{region}/archive/{source}/{image type}/{scan}.{png|jpg}`.
  Archived MTG FCI raw scans that have not been rendered yet are still
  listed and rendered on request.
- `GET /api/archive` and `POST /api/storage` also cover the rendered-image
  archive (`render_all`, `render_retention_hours`, `render_max_storage_mb`).
- Archived images of a region are discarded when its cut-out changes
  (position, radius, size, country borders).

## [1.0.0] — 2026-09-30

First public release.

### Added

- Versioning: `__version__` in `src/ha_satellite/__init__.py` and
  `pyproject.toml`, shown in the web UI footer and via `GET /api/version`;
  release tags `vX.Y.Z` are built into image tags `X.Y.Z`, `X.Y`, `latest`.
- Optional Watchtower service in `docker-compose.yml` for automatic updates
  (label-scoped, configurable poll interval, `--cleanup`).
- Local, persistent configuration: all state in a host directory mounted to
  `/data`, configurable via `HOST_DATA_DIR` (default `./data`).

### Changed

- Web UI, code and documentation translated to English.
- Public GHCR image; no registry login required.
