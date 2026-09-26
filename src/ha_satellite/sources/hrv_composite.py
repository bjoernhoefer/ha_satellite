"""Eigener Satpy-Compositor: Echtfarben mit HRV-Helligkeit geschärft.

Wird ausschließlich über ``satpy_config/composites/seviri.yaml`` im
Render-Kindprozess geladen (importiert Satpy direkt).
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from satpy.composites.core import GenericCompositor, IncompatibleAreas

# Obergrenze des Schärfungsfaktors: über dunklem Meer ist HRV (enthält
# blaues Streulicht) deutlich heller als VIS006/VIS008; 1.5 (Satpy-Default
# bei RatioSharpenedRGB) ließ dort Wolken unscharf.
MAX_RATIO = 3.0


class HrvLuminanceSharpenedRGB(GenericCompositor):
    """Skaliert alle drei Farbkanäle mit ``HRV / mean(VIS006, VIS008)``.

    Der Nenner liegt im selben ~3-km-Raster wie die Farbkanäle, der Faktor
    hebt deren Blockstruktur also auf und bringt die ~1-km-Details des HRV
    ein, ohne den Farbton zu verändern (anders als ``RatioSharpenedRGB``,
    das einen Kanal durch HRV ersetzt und über Meer einen Magentastich
    erzeugt).
    """

    def __call__(self, datasets, optional_datasets=None, **info):
        if len(datasets) != 3:
            raise ValueError(f"Erwartet 3 Datensätze, erhalten {len(datasets)}")
        optional_datasets = tuple(optional_datasets or ())
        shapes = {d.shape for d in (*datasets, *optional_datasets)}
        if len(shapes) > 1:
            raise IncompatibleAreas("HRV-Schärfung erst nach dem Resampling möglich")
        red, green, blue, *hrv = self.match_data_arrays((*datasets, *optional_datasets))
        if hrv:
            with np.errstate(divide="ignore", invalid="ignore"):
                ratio = hrv[0] / ((green + blue) / 2)
            ratio = ratio.where(np.isfinite(ratio) & (ratio > 0), 1.0).clip(0, MAX_RATIO)
            with xr.set_options(keep_attrs=True):
                red, green, blue = red * ratio, green * ratio, blue * ratio
        result = super().__call__((red, green, blue), **info)
        result.attrs.pop("units", None)
        return result
