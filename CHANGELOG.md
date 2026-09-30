# Changelog

All notable changes to this project are documented in this file.
The project follows [Semantic Versioning](https://semver.org/).

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
