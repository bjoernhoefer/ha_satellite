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
# macht.
#
# Stand Phase 2: Inzwischen liefert PyPI für alle diese Pakete fertige
# manylinux-aarch64-Wheels, während conda-forge für trollimage (Satpy-
# Abhängigkeit) keine aktuelle linux-aarch64-Version führt. Die Satpy-Kette
# kommt daher per pip aus den Wheels; conda-forge stellt nur Python selbst.
FROM mambaorg/micromamba:1.5-bookworm

USER root
WORKDIR /app

ENV MAMBA_DOCKERFILE_ACTIVATE=1 \
    PYTHONUNBUFFERED=1 \
    HA_SATELLITE_CONFIG=/data/config.yaml \
    SATPY_DATA_DIR=/data/satpy \
    XDG_CACHE_HOME=/data/cache/xdg

RUN micromamba install -y -n base -c conda-forge python=3.12 pip && \
    micromamba clean --all --yes

COPY pyproject.toml README.md /app/
COPY src /app/src

# --only-binary: nie aus dem Sdist kompilieren (auf dem Pi/unter QEMU
# extrem langsam) - fehlt ein Wheel, soll der Build sofort scheitern.
RUN micromamba run -n base pip install --no-cache-dir \
        --only-binary=numpy,pyproj,pyresample,pykdtree,trollimage,pillow .

# Healthchecks und `docker exec` durchlaufen das Entrypoint nicht, d. h. die
# conda-Umgebung ist dort nicht aktiviert und `python3` läge nicht im PATH.
RUN ln -sf /opt/conda/bin/python /usr/local/bin/python3

RUN mkdir -p /data/frames /data/cache /data/satpy && chown -R $MAMBA_USER:$MAMBA_USER /data

COPY docker/entrypoint.sh /usr/local/bin/ha_satellite-entrypoint.sh
RUN chmod +x /usr/local/bin/ha_satellite-entrypoint.sh

EXPOSE 6060
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD /usr/local/bin/python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:6060/healthz')"

ENTRYPOINT ["/usr/local/bin/ha_satellite-entrypoint.sh"]
CMD ["uvicorn", "ha_satellite.main:app", "--host", "0.0.0.0", "--port", "6060"]
