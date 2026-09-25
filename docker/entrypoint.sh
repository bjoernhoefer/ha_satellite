#!/usr/bin/env bash
# Startet standardmäßig als root (Docker-Default), da ein per Bind-Mount
# eingehängtes /data-Volume die im Image gesetzte Ownership überschreibt -
# ein "chown" im Dockerfile allein reicht deshalb nicht aus. Dieses Skript
# gleicht die Ownership zur Laufzeit an und lässt die eigentliche
# Anwendung anschließend mit reduzierten Rechten (mambauser) laufen.
set -euo pipefail

if [ "$(id -u)" = "0" ]; then
    mkdir -p /data/frames
    chown -R mambauser:mambauser /data
    exec setpriv --reuid=mambauser --regid=mambauser --init-groups \
        /usr/local/bin/_entrypoint.sh "$@"
fi

exec /usr/local/bin/_entrypoint.sh "$@"
