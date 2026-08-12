"""
Clip the "clean" (pre-proposal, existing-conditions) citywide UTCI surface to
our site grid, for comparison against the "Version 1" proposed-planting
scenario this tool otherwise models. Source: a large Fall-clear-sky hourly
UTCI dataset covering most of Austin, same CRS/resolution as our site data
(EPSG:6343, 2m) -- a direct pixel-aligned crop, no reprojection needed.

Only UTCI is available for the clean scenario (no matching Tmrt), so it's a
static reference/comparison layer, not independently interactive.
"""
import numpy as np
import rasterio
from rasterio.windows import from_bounds

from common import PATHS, GROUND_VALUE

CLEAN_SOURCE = r"E:/UHS Research/Fall_clear_UTCI_merged/Fall_clear_UTCI_merged/UTCI_1300.tif"
OUT_PATH = PATHS["DSM"].parent / "UTCI_clean_baseline.tif"


def main():
    with rasterio.open(PATHS["DSM"]) as ref:
        site_bounds, site_transform, site_shape, profile = ref.bounds, ref.transform, ref.shape, ref.profile

    with rasterio.open(CLEAN_SOURCE) as src:
        assert src.crs == profile["crs"], f"CRS mismatch: {src.crs} vs {profile['crs']}"
        assert src.res == (2.0, 2.0), f"unexpected resolution: {src.res}"
        window = from_bounds(*site_bounds, transform=src.transform)
        assert abs(window.width - site_shape[1]) < 0.01 and abs(window.height - site_shape[0]) < 0.01, \
            f"window size {window.width}x{window.height} doesn't match site grid {site_shape}"
        clean = src.read(1, window=window, boundless=True, fill_value=0.0).astype(np.float32)

    with rasterio.open(PATHS["buildings"]) as src:
        ground_mask = src.read(1) == GROUND_VALUE

    # 0.0 is this dataset's own nodata/excluded-pixel sentinel (86% of exact-zero
    # cells fall on our building mask). Use the intersection of "source says
    # valid" and "our own ground mask says valid" to avoid edge-registration
    # artifacts between the two independently-produced building masks.
    valid = (clean > 0.1) & ground_mask
    print(f"Clean baseline: {valid.sum()} valid cells ({100*valid.sum()/clean.size:.1f}%), "
          f"mean UTCI {clean[valid].mean():.2f} C, range {clean[valid].min():.2f} to {clean[valid].max():.2f} C")

    with rasterio.open(PATHS["UTCI"]) as src:
        v1 = np.ma.filled(src.read(1, masked=True).astype(np.float32), np.nan)
    v1_valid = ~np.isnan(v1) & ground_mask
    both_valid = valid & v1_valid
    print(f"Version 1 (current tool baseline): mean UTCI {v1[v1_valid].mean():.2f} C over {v1_valid.sum()} cells")
    print(f"Cells with both scenarios valid: {both_valid.sum()}, "
          f"mean delta (clean - v1): {(clean[both_valid] - v1[both_valid]).mean():.2f} C "
          f"(positive = existing proposal already cooling things)")

    out = np.where(valid, clean, -9999.0).astype(np.float32)
    out_profile = profile.copy()
    out_profile.update(dtype="float32", nodata=-9999.0, count=1)
    with rasterio.open(OUT_PATH, "w", **out_profile) as dst:
        dst.write(out, 1)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
