"""
Build a corrected UMEP land cover raster for the SOLWEIG rerun, from the real
Austin 2017 LULC classification instead of SOLWEIG's flawed demforbuild
(DSM-DEM threshold) building detection.

LULC_final.tif already uses UMEP's land-cover codes almost exactly (it's
literally named *_UMEP): 1=Paved, 2=Buildings, 3=Evergreen trees,
4=Deciduous trees, 5=Grass, 6=Bare soil, 7=Water. UMEP's SOLWEIG ground-cover
scheme has no "tree" class (canopy shading comes from CDSM elevation, not
land cover) -- classes 3/4 are reclassified to Grass (5), a reasonable ground
surface under canopy in this park context.

Cross-check: this LULC says buildings = 21.6% of the site, matching the real
building-vector footprint area (22.0%) almost exactly -- strong confirmation
this is trustworthy, versus the raster threshold's 65.4%.
"""
import numpy as np
import rasterio
from rasterio.warp import reproject, Resampling

from common import PATHS

LULC_PATH = r"E:/Jamestown_Summer_Clipped/Austin_land_cover_2017_UMEP/Austin_land_cover_2017_UMEP/LULC_final.tif"
OUT_PATH = PATHS["DSM"].parent.parent / "rerun" / "landcover.tif"

# UMEP codes this dataset already matches: 1=Paved(asphalt), 2=Buildings/Roofs,
# 5=Grass_unmanaged, 6=Bare_soil, 7=Water. Reclassify tree canopy (3,4) -> grass.
RECLASSIFY = {3: 5, 4: 5}


def main():
    with rasterio.open(PATHS["DSM"]) as ref:
        dst_crs, dst_transform, dst_shape = ref.crs, ref.transform, ref.shape

    dst = np.zeros(dst_shape, dtype=np.float32)
    with rasterio.open(LULC_PATH) as src:
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.nearest,  # categorical data -- never interpolate
        )

    for old, new in RECLASSIFY.items():
        dst[dst == old] = new

    vals, counts = np.unique(dst, return_counts=True)
    print("Reclassified land cover distribution:")
    for v, c in zip(vals, counts):
        print(f"  code {v:.0f}: {c} px ({100*c/dst.size:.1f}%)")

    profile = {
        "driver": "GTiff", "dtype": "float32", "nodata": None,
        "width": dst_shape[1], "height": dst_shape[0],
        "count": 1, "crs": dst_crs, "transform": dst_transform,
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(OUT_PATH, "w", **profile) as out:
        out.write(dst, 1)
    print(f"Wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
