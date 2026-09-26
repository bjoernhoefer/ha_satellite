#!/usr/bin/env bash
# Startet standardmäßig als root (Docker-Default), da ein per Bind-Mount
# eingehängtes /data-Volume die im Image gesetzte Ownership überschreibt -
# ein "chown" im Dockerfile allein reicht deshalb nicht aus. Dieses Skript
# gleicht die Ownership zur Laufzeit an und lässt die eigentliche
# Anwendung anschließend mit reduzierten Rechten (mambauser) laufen.
#
# PUID/PGID (optional): UID/GID, mit der der Dienst läuft. Sollte zum
# Besitzer der Host-Verzeichnisse passen, in die Bilder geschrieben werden
# (z. B. /mnt/data/ha_satellite), sonst schlägt der Speicherort-Wechsel in
# der Web-UI mit "nicht beschreibbar" fehl.
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    if [ -n "${PGID:-}" ] && [ "$PGID" != "$(id -g mambauser)" ]; then
        groupmod -o -g "$PGID" mambauser || echo "WARNUNG: PGID=$PGID konnte nicht gesetzt werden" >&2
    fi
    if [ -n "${PUID:-}" ] && [ "$PUID" != "$(id -u mambauser)" ]; then
        usermod -o -u "$PUID" mambauser || echo "WARNUNG: PUID=$PUID konnte nicht gesetzt werden" >&2
    fi

    mkdir -p /data/frames
    chown -R mambauser:mambauser /data

    # In der Web-UI gewählten Speicherort (falls vorhanden) mitziehen, damit
    # er nach einer PUID/PGID-Änderung beschreibbar bleibt.
    frames_dir="$(/usr/local/bin/python3 - <<'PY' 2>/dev/null || true
import os, yaml
path = os.environ.get("HA_SATELLITE_CONFIG", "/data/config.yaml")
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
            echo "WARNUNG: Ownership von $frames_dir konnte nicht angepasst werden" >&2
    fi

    exec setpriv --reuid=mambauser --regid=mambauser --init-groups \
        /usr/local/bin/_entrypoint.sh "$@"
fi

exec /usr/local/bin/_entrypoint.sh "$@"
