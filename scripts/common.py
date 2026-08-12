import os
from pathlib import Path

import numpy as np
import rasterio
from scipy import ndimage

PROJECT_DIR = Path(__file__).resolve().parent.parent
# UTCI_SCENARIO selects which data/ folder the pipeline scripts read/write --
# "" (default) = data/ (Version 1, the proposed-planting scenario), "clean" =
# data_clean/ (existing conditions, no proposed trees). Same grid/DSM/DEM/
# landcover/buildings either way; only CDSM/Tmrt/UTCI differ per scenario.
SCENARIO = os.environ.get("UTCI_SCENARIO", "")
DATA_DIR = PROJECT_DIR / (f"data_{SCENARIO}" if SCENARIO else "data")

PATHS = {
    "DSM": DATA_DIR / "DSM.tif",
    "DEM": DATA_DIR / "DEM.tif",
    "CDSM": DATA_DIR / "CDSM.tif",
    "Tmrt": DATA_DIR / "Tmrt.tif",
    "UTCI": DATA_DIR / "UTCI.tif",
    "buildings": DATA_DIR / "buildings.tif",
    "landcover": DATA_DIR / "landcover.tif",
    "Shadow_reference": DATA_DIR / "Shadow_reference.tif",
}

# UMEP land-cover codes, from Austin_land_cover_2017_UMEP / solweig_parameters.json
LC_PAVED, LC_BUILDING, LC_GRASS, LC_BARE_SOIL, LC_WATER = 1, 2, 5, 6, 7

# Site + design-hour constants, from Test_MRT_2/configsolweig.ini and metforcing.txt
LAT = 30.353276247561688
LON = -97.71699815949414
UTC_OFFSET_HOURS = -6
YEAR = 2020
DAY_OF_YEAR = 277
HOUR_LOCAL = 13

TA_C = 26.71
RH_PCT = 48.44
WIND_MS = 2.38

# buildings.tif convention confirmed by inspection: 1 = ground/pedestrian cell, 0 = building rooftop
GROUND_VALUE = 1
BUILDING_VALUE = 0

TREE_TRANSMISSIVITY = 0.05  # from solweig_parameters.json Tree_settings.Transmissivity


def load_corrected_canopy_and_buildingtop(cdsm_source=None):
    """
    CDSM only has real height values for 11.4% of cells (huge LiDAR gaps).
    After the SOLWEIG rerun with a land-cover-derived building mask (see memory:
    open-todo-building-mask-overclassification), a lot of real ground cells have
    a DSM-DEM elevation bump >1m with no recorded CDSM value. Earlier version of
    this function attributed ALL such bumps to inferred tree canopy -- but that
    wrongly painted elevated PAVED infrastructure (a highway/overpass segment on
    the west side of the site: 12,760 cells, 41% of all paved area, mean inferred
    "canopy" height 8.4m) as trees, which also fed wrong physics into the actual
    SOLWEIG rerun (highway modelled as transmissive shaded parkland instead of
    full-sun pavement). Fixed by only inferring canopy on land-cover classes
    where vegetation is plausible (grass, bare soil) -- never paved or water.

    Elsewhere (paved bumps, building edges, anything not attributed to canopy),
    the real DSM relief is preserved as-is via building_top = dsm - canopy_height
    (only subtracting the height we've explicitly decided is vegetation).

    cdsm_source: path to an alternate raw CDSM to correct (e.g. the "clean"
    pre-proposal CDSM with the 111 proposed trees removed). Defaults to
    PATHS["CDSM"].
    """
    with rasterio.open(PATHS["DSM"]) as src:
        dsm = src.read(1).astype(np.float64)
    with rasterio.open(PATHS["DEM"]) as src:
        dem = src.read(1).astype(np.float64)
    with rasterio.open(cdsm_source or PATHS["CDSM"]) as src:
        cdsm_raw = np.ma.filled(src.read(1, masked=True).astype(np.float64), 0.0)
        cdsm_raw[cdsm_raw < 0] = 0.0
    with rasterio.open(PATHS["buildings"]) as src:
        ground_mask = src.read(1) == GROUND_VALUE
    with rasterio.open(PATHS["landcover"]) as src:
        landcover = src.read(1)

    height_bump = dsm - dem
    has_real_cdsm = cdsm_raw > 0.5
    vegetation_plausible = (landcover == LC_GRASS) | (landcover == LC_BARE_SOIL)

    # exclude a 1-pixel buffer around real buildings: the land-cover raster (1m,
    # reprojected) and the DSM's actual building silhouette don't register
    # perfectly, so DSM height right at a building edge can spill into a
    # "ground" cell there. Inferring that as canopy mislabels real building
    # edges as transmissive tree shadow.
    building_adjacent = ndimage.binary_dilation(~ground_mask, iterations=1) & ground_mask

    infer_canopy = (ground_mask & (~has_real_cdsm) & (height_bump > 1.0)
                     & (~building_adjacent) & vegetation_plausible)

    canopy_top_height = cdsm_raw.copy()
    canopy_top_height[infer_canopy] = height_bump[infer_canopy]

    building_top = dsm - canopy_top_height

    return canopy_top_height, building_top
