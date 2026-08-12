"""
Build the "clean" (pre-proposal, existing-conditions-only) CDSM by removing
the 111 proposed tree footprints from CDSM_Merged.tif. Proposed_Trees.gpkg
gives exact point locations + canopy diameter for each proposed tree; 98 of
111 land directly on real recorded CDSM canopy, and all 111 share identical
Height=14.0m / canopy diameter=20.0m (a landscape-plan fingerprint -- uniform
nursery stock, not organic existing-tree variation). This confirms
CDSM_Merged.tif = existing trees + these 111 proposed trees.

Zeroing their canopy footprints (radius = diameter/2) recovers a genuine
existing-conditions CDSM, letting the "clean" scenario go through the exact
same validated pipeline as "Version 1" (SVF -> SOLWEIG -> Spatial TC) instead
of being an approximate citywide-dataset reference layer.
"""
import geopandas as gpd
import numpy as np
import rasterio

from common import PATHS

PROPOSED_TREES_PATH = r"E:/Jamestown_Summer_Clipped/Proposed_Trees.gpkg"
OUT_PATH = PATHS["DSM"].parent.parent / "rerun" / "CDSM_clean_raw.tif"


def main():
    gdf = gpd.read_file(PROPOSED_TREES_PATH)
    print(f"{len(gdf)} proposed trees to remove")

    with rasterio.open(r"E:/Jamestown_Summer_Clipped/CDSM_Merged.tif") as src:
        cdsm = np.ma.filled(src.read(1, masked=True).astype(np.float32), 0.0)
        profile = src.profile
        transform = src.transform

    rows, cols = cdsm.shape
    inv = ~transform
    row_idx, col_idx = np.indices((rows, cols))

    n_removed_cells = 0
    for _, tree in gdf.iterrows():
        col_c, row_c = inv * (tree.geometry.x, tree.geometry.y)
        radius_px = (tree["Sanopy_Diameter"] / 2.0) / 2.0  # canopy radius in cells (2m cells)
        dist = np.sqrt((row_idx - row_c) ** 2 + (col_idx - col_c) ** 2)
        mask = dist <= radius_px
        n_removed_cells += (mask & (cdsm > 0.5)).sum()
        cdsm[mask] = 0.0

    print(f"Zeroed {n_removed_cells} cells that had real canopy within a proposed-tree footprint")
    with rasterio.open(OUT_PATH, "w", **profile) as dst:
        dst.write(cdsm, 1)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
