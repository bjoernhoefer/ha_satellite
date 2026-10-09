"""Custom Satpy compositor: natural color sharpened with HRV brightness.

Loaded exclusively via ``satpy_config/composites/seviri.yaml`` in the
render child process (imports Satpy directly).
"""

from __future__ import annotations

import numpy as np
import xarray as xr
from satpy.composites.core import GenericCompositor, IncompatibleAreas

# Upper bound of the sharpening factor: over dark sea HRV (which contains
# blue scattered light) is much brighter than VIS006/VIS008; 1.5 (Satpy's
# default for RatioSharpenedRGB) left clouds blurry there.
MAX_RATIO = 3.0


class HrvLuminanceSharpenedRGB(GenericCompositor):
    """Scales all three color channels by ``HRV / mean(VIS006, VIS008)``.

    The denominator is on the same ~3 km grid as the color channels, so the
    factor cancels their block structure and adds the ~1 km detail of HRV
    without changing the hue (unlike ``RatioSharpenedRGB``, which replaces
    one channel with HRV and produces a magenta tint over sea).
    """

    def __call__(self, datasets, optional_datasets=None, **info):
        if len(datasets) != 3:
            raise ValueError(f"Expected 3 datasets, got {len(datasets)}")
        optional_datasets = tuple(optional_datasets or ())
        shapes = {d.shape for d in (*datasets, *optional_datasets)}
        if len(shapes) > 1:
            raise IncompatibleAreas("HRV sharpening is only possible after resampling")
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
