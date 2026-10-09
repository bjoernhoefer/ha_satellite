#!/usr/bin/env bash
# Starts as root by default (Docker default), because a bind-mounted
# /data volume overrides the ownership set in the image - a "chown" in the
# Dockerfile alone is therefore not enough. This script fixes the ownership
# at runtime and then runs the actual application with reduced privileges
# (mambauser).
#
# PUID/PGID (optional): UID/GID the service runs as. Should match the owner
# of the host directories images are written to (e.g.
# /mnt/data/ha_satimage), otherwise changing the storage location in the
# web UI fails with "not writable".
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    if [ -n "${PGID:-}" ] && [ "$PGID" != "$(id -g mambauser)" ]; then
        groupmod -o -g "$PGID" mambauser || echo "WARNING: could not set PGID=$PGID" >&2
    fi
    if [ -n "${PUID:-}" ] && [ "$PUID" != "$(id -u mambauser)" ]; then
        usermod -o -u "$PUID" mambauser || echo "WARNING: could not set PUID=$PUID" >&2
    fi

    mkdir -p /data/frames
    chown -R mambauser:mambauser /data

    # Also fix the storage location chosen in the web UI (if any) so it
    # stays writable after a PUID/PGID change.
    frames_dir="$(/usr/local/bin/python3 - <<'PY' 2>/dev/null || true
import os, yaml
path = os.environ.get("HA_SATIMAGE_CONFIG", "/data/config.yaml")
try:
    with open(path, encoding="utf-8") as fh:
        config = yaml.safe_load(fh) or {}
    print((config.get("storage") or {}).get("frames_dir") or "")
except OSError:
    pass
PY
)"
    if [ -n "$frames_dir" ] && [ -d "$frames_dir" ]; then
        chown -R mambauser:mambauser "$frames_dir" || \
            echo "WARNING: could not adjust ownership of $frames_dir" >&2
    fi

    exec setpriv --reuid=mambauser --regid=mambauser --init-groups \
        /usr/local/bin/_entrypoint.sh "$@"
fi

exec /usr/local/bin/_entrypoint.sh "$@"
