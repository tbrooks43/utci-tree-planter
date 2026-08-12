"""
Write a denser CDSM raster inferring canopy height from unexplained DSM-DEM
bumps on confirmed-ground cells (see common.load_corrected_canopy_and_buildingtop),
so a SOLWEIG rerun's own shadow-casting physics -- not just the pedestrian
output mask -- correctly treats these real, LiDAR-missed trees as transmissive
vegetation obstructions instead of opaque building obstructions.
"""
import numpy as np
import rasterio

from common import PATHS, load_corrected_canopy_and_buildingtop

OUT_PATH = PATHS["DSM"].parent.parent / "rerun" / "CDSM_corrected.tif"


def main():
    canopy_height, _ = load_corrected_canopy_and_buildingtop()
    with rasterio.open(PATHS["CDSM"]) as src:
        profile = src.profile
        orig_cdsm = np.ma.filled(src.read(1, masked=True).astype(np.float64), 0.0)
        orig_cdsm[orig_cdsm < 0] = 0.0

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(OUT_PATH, "w", **profile) as dst:
        dst.write(canopy_height.astype(profile["dtype"]), 1)
    print(f"Wrote {OUT_PATH}, canopy height range {canopy_height.min():.2f} to {canopy_height.max():.2f} m")
    print(f"  nonzero canopy cells: {(canopy_height > 0.5).sum()} (original CDSM had {(orig_cdsm > 0.5).sum()})")


if __name__ == "__main__":
    main()
