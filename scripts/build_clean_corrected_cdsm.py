"""
Apply the same LiDAR-gap inference used for Version 1 (see build_corrected_cdsm.py)
to the "clean" (pre-proposal) raw CDSM, producing a fully corrected clean-scenario
CDSM ready for its own SVF/SOLWEIG/SpatialTC rerun.
"""
import numpy as np
import rasterio

from common import PATHS, load_corrected_canopy_and_buildingtop

CLEAN_RAW = PATHS["DSM"].parent.parent / "rerun" / "CDSM_clean_raw.tif"
OUT_PATH = PATHS["DSM"].parent.parent / "rerun" / "CDSM_clean_corrected.tif"


def main():
    canopy_height, _ = load_corrected_canopy_and_buildingtop(cdsm_source=CLEAN_RAW)
    with rasterio.open(CLEAN_RAW) as src:
        profile = src.profile

    with rasterio.open(OUT_PATH, "w", **profile) as dst:
        dst.write(canopy_height.astype(profile["dtype"]), 1)
    print(f"Wrote {OUT_PATH}, canopy height range {canopy_height.min():.2f} to {canopy_height.max():.2f} m, "
          f"nonzero cells: {(canopy_height > 0.5).sum()}")

    # sanity: confirm the 111 proposed-tree footprints didn't get reintroduced by inference
    import geopandas as gpd
    gdf = gpd.read_file(r"E:/Jamestown_Summer_Clipped/Proposed_Trees.gpkg")
    with rasterio.open(CLEAN_RAW) as src:
        transform = src.transform
    inv = ~transform
    reintroduced = 0
    for geom in gdf.geometry:
        col, row = inv * (geom.x, geom.y)
        r, c = int(row), int(col)
        if 0 <= r < canopy_height.shape[0] and 0 <= c < canopy_height.shape[1]:
            if canopy_height[r, c] > 1.5:
                reintroduced += 1
    print(f"Proposed-tree locations with canopy >1.5m after correction (should be ~0): {reintroduced}/{len(gdf)}")


if __name__ == "__main__":
    main()
