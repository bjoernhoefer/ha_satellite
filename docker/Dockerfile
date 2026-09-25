# syntax=docker/dockerfile:1

# Basis-Image: mambaorg/micromamba statt "python:3.12-slim" + pip.
#
# Begründung (siehe auch HISTORY.md): Die geplante Satpy-Rendering-Kette
# (Phase 2) benötigt pyresample, pykdtree und pyproj. Diese Pakete
# kompilieren auf ARM64 (Raspberry Pi 5) aus dem PyPI-Sdist heraus sehr
# langsam (pykdtree/pyresample bauen native Erweiterungen, pyproj bringt
# PROJ als C-Bibliothek mit) und benötigen zusätzliche System-Header, die
# in schlanken Images nicht vorhanden sind. conda-forge stellt für all
# diese Pakete fertige linux-aarch64-Builds bereit, wodurch micromamba
# (ein schlanker, statisch gelinkter conda-Ersatz ohne den Overhead von
# Anaconda) den ARM64-Build drastisch beschleunigt und reproduzierbar
# macht. Für das aktuelle Grundgerüst (ohne Satpy) sind ausschließlich
# reine Python-Pakete nötig, die genauso gut per pip liefen - das Image
# wird aber bereits jetzt auf micromamba/conda-forge umgestellt, damit
# der ARM64-Build frühzeitig verifiziert ist und Phase 2 ohne Wechsel des
# Basis-Images auskommt.
FROM mambaorg/micromamba:1.5-bookworm

USER root
WORKDIR /app

ENV MAMBA_DOCKERFILE_ACTIVATE=1 \
    PYTHONUNBUFFERED=1 \
    HA_SATELLITE_CONFIG=/data/config.yaml

# Python-Interpreter aus conda-forge (liefert vorgebaute Wheels/Binaries
# für alle unterstützten Architekturen inkl. linux-aarch64).
RUN micromamba install -y -n base -c conda-forge python=3.12 pip && \
    micromamba clean --all --yes

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN micromamba run -n base pip install --no-cache-dir .

RUN mkdir -p /data/frames && chown -R $MAMBA_USER:$MAMBA_USER /data

COPY docker/entrypoint.sh /usr/local/bin/ha_satellite-entrypoint.sh
RUN chmod +x /usr/local/bin/ha_satellite-entrypoint.sh

EXPOSE 8080
VOLUME ["/data"]

ENTRYPOINT ["/usr/local/bin/ha_satellite-entrypoint.sh"]
CMD ["uvicorn", "ha_satellite.main:app", "--host", "0.0.0.0", "--port", "8080"]
