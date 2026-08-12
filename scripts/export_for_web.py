"""
Export the offline-Python results (stage 1+2+3) into flat binary buffers +
a manifest.json the browser can fetch directly. This single site is one small
tile (204x317 cells) so we skip the full-Austin PNG-tiling scheme described in
CLAUDE.md (that's designed for ~200M-cell citywide coverage); Float32Array
buffers are simpler and plenty fast at this scale.

Local scene coordinates: x = east+, z = south+, origin at grid center. This
matches the shadow-direction formula in CLAUDE.md (shx=-sin(az), shz=cos(az))
with no sign flips, and matches the row/col convention used in stage 1/2
(row increases south, col increases east).

Two scenarios share the same grid/DSM/DEM/landcover/buildings (ground mask is
identical -- it's land-cover derived, independent of CDSM):
  - "v1" (data/): the proposed-planting scenario ("Version 1"), CDSM includes
    the 111 proposed trees from Proposed_Trees.gpkg.
  - "clean" (data_clean/): existing conditions only, those 111 trees removed.
Each gets its own tmrtSun/tmrtShade/tmrtMeasured/uncertainty/UTCI-LUT/existing-
tree layer; groundMask and building geometry are shared.
"""
import json

import geopandas as gpd
import numpy as np
import rasterio
from scipy import ndimage

from common import PROJECT_DIR, LAT, LON, TA_C, RH_PCT, WIND_MS, TREE_TRANSMISSIVITY, GROUND_VALUE

OUT_DIR = PROJECT_DIR / "web" / "data"
CELL_SIZE = 2.0
BUILDING_VECTOR_PATH = r"E:/Jamestown_Summer_Clipped/Building_Vector.fgb"

SCENARIOS = {
    "v1": {"dir": PROJECT_DIR / "data", "label": "Version 1 (proposed planting)"},
    "clean": {"dir": PROJECT_DIR / "data_clean", "label": "Clean (existing conditions)"},
}


def read(path):
    with rasterio.open(path) as src:
        arr = src.read(1, masked=True)
        return np.ma.filled(arr.astype(np.float64), np.nan), src.profile


def extract_buildings(rows, cols, transform):
    """
    Real building footprints from Building_Vector.fgb (334 features, ROOF_HEIGHT
    field). Visualization-only; does NOT touch the thermal classification, which
    stays tied to buildings.tif (identical between scenarios -- land-cover
    derived, independent of CDSM).
    """
    origin_x, origin_y = transform.c, transform.f
    width, height = cols * CELL_SIZE, rows * CELL_SIZE

    gdf = gpd.read_file(BUILDING_VECTOR_PATH)
    gdf = gdf[gdf["ROOF_HEIGHT"].notna() & (gdf["ROOF_HEIGHT"] > 0.5)]

    site_bounds_x = (origin_x, origin_x + width)
    site_bounds_y = (origin_y - height, origin_y)
    gdf = gdf.cx[site_bounds_x[0]:site_bounds_x[1], site_bounds_y[0]:site_bounds_y[1]]

    def to_local(coords):
        return [[x - origin_x - width / 2.0, origin_y - y - height / 2.0] for x, y in coords]

    buildings = []
    for _, row in gdf.iterrows():
        h = round(float(row["ROOF_HEIGHT"]), 2)
        geom = row.geometry
        polys = geom.geoms if geom.geom_type == "MultiPolygon" else [geom]
        for poly in polys:
            ring = to_local(list(poly.exterior.coords))
            if len(ring) >= 3:
                buildings.append({"h": h, "ring": ring})
    print(f"  {len(buildings)} real building polygons within site bounds (from {len(gdf)} features)")
    return buildings


def extract_existing_trees(cdsm, ground_mask, rows, cols):
    """
    Individual tree crowns via local-maxima peak detection in the canopy height
    field, NOT connected-component blob area -- real canopy is often contiguous
    (touching/overlapping crowns, tree rows), so a naive connected-component
    label merges an entire grove into one "tree" with an absurd equivalent-
    circle radius (observed: one 52m-radius blob from a cluster of real trees).
    Peak detection finds one marker per local crown apex instead.

    Restricted to ground_mask cells -- a small number of real recorded CDSM
    peaks land on building cells and rendered as a tree growing out of a
    building.
    """
    footprint = np.ones((5, 5))  # ~10m x 10m window -> min spacing between detected trees
    local_max = ndimage.maximum_filter(cdsm, footprint=footprint) == cdsm
    is_tree = local_max & (cdsm > 1.5) & ground_mask
    rr, cc = np.where(is_tree)

    trees = []
    for r, c in zip(rr, cc):
        h = float(cdsm[r, c])
        radius = max(1.5, min(8.0, h * 0.4))  # typical crown-radius/height ratio, clamped to plausible single-tree bounds
        cx = (c + 0.5) * CELL_SIZE - cols * CELL_SIZE / 2.0
        cz = (r + 0.5) * CELL_SIZE - rows * CELL_SIZE / 2.0
        trees.append({"cx": round(cx, 2), "cz": round(cz, 2), "crown": round(radius, 2), "height": round(h, 2)})
    return trees


def export_scenario(name, data_dir, ground_mask, rows, cols):
    tmrt_sun, _ = read(data_dir / "tmrtSun.tif")
    tmrt_shade, _ = read(data_dir / "tmrtShade.tif")
    uncertainty, _ = read(data_dir / "endpoint_uncertainty.tif")
    tmrt_measured, _ = read(data_dir / "Tmrt.tif")
    cdsm, _ = read(data_dir / "CDSM.tif")
    cdsm = np.nan_to_num(cdsm, nan=0.0)
    cdsm[cdsm < 0] = 0.0

    fill = np.nanmean(tmrt_sun[ground_mask])
    tmrt_sun_f = np.where(ground_mask, tmrt_sun, fill).astype(np.float32)
    tmrt_shade_f = np.where(ground_mask, tmrt_shade, fill).astype(np.float32)
    tmrt_measured_f = np.where(ground_mask, tmrt_measured, fill).astype(np.float32)
    uncertainty_f = np.where(ground_mask, uncertainty, 0.0).astype(np.float32)

    tmrt_sun_f.tofile(OUT_DIR / f"tmrtSun_{name}.bin")
    tmrt_shade_f.tofile(OUT_DIR / f"tmrtShade_{name}.bin")
    tmrt_measured_f.tofile(OUT_DIR / f"tmrtMeasured_{name}.bin")
    uncertainty_f.tofile(OUT_DIR / f"uncertainty_{name}.bin")
    print(f"  [{name}] wrote tmrtSun/tmrtShade/tmrtMeasured/uncertainty .bin, "
          f"mean measured UTCI baseline (ground cells): "
          f"{np.nanmean(np.where(ground_mask, tmrt_measured, np.nan)):.2f} C Tmrt")

    existing_trees = extract_existing_trees(cdsm, ground_mask, rows, cols)
    print(f"  [{name}] {len(existing_trees)} existing tree crowns")

    with open(data_dir / "utci_lut.json") as f:
        lut = json.load(f)

    with rasterio.open(data_dir / "UTCI.tif") as src:
        u = src.read(1, masked=True)
        mean_utci = float(np.ma.filled(u, np.nan)[ground_mask][~np.isnan(np.ma.filled(u, np.nan)[ground_mask])].mean())

    return {
        "label": SCENARIOS[name]["label"],
        "utciLut": lut,
        "existingTrees": existing_trees,
        "meanUtciBaseline": round(mean_utci, 2),
    }


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    v1_dir = SCENARIOS["v1"]["dir"]
    with rasterio.open(v1_dir / "DSM.tif") as src:
        profile = src.profile
        rows, cols = src.shape
    with rasterio.open(v1_dir / "buildings.tif") as src:
        ground_mask = src.read(1) == GROUND_VALUE

    # ground mask is identical between scenarios (land-cover derived, not CDSM-dependent) -- verify
    with rasterio.open(SCENARIOS["clean"]["dir"] / "buildings.tif") as src:
        clean_ground_mask = src.read(1) == GROUND_VALUE
    assert np.array_equal(ground_mask, clean_ground_mask), "ground mask differs between scenarios -- unexpected"

    print(f"Grid: {rows} rows x {cols} cols, {CELL_SIZE} m cells ({cols*CELL_SIZE:.0f} x {rows*CELL_SIZE:.0f} m)")

    ground_mask_u8 = ground_mask.astype(np.uint8)
    ground_mask_u8.tofile(OUT_DIR / "groundMask.bin")

    buildings = extract_buildings(rows, cols, profile["transform"])

    scenarios_out = {}
    for name in SCENARIOS:
        print(f"Exporting scenario '{name}' ({SCENARIOS[name]['dir']})...")
        scenarios_out[name] = export_scenario(name, SCENARIOS[name]["dir"], ground_mask, rows, cols)

    manifest = {
        "grid": {"rows": rows, "cols": cols, "cellSize": CELL_SIZE},
        "site": {
            "lat": LAT, "lon": LON,
            "hourLabel": "13:00 (2020-10-03)",
            "airTempC": TA_C, "rhPct": RH_PCT, "windMs": WIND_MS,
            "sunAzimuthDeg": 197.27, "sunAltitudeDeg": 53.98,
            "existingTreeTransmissivity": TREE_TRANSMISSIVITY,
        },
        "buildings": buildings,
        "scenarios": scenarios_out,
        "defaultScenario": "v1",
        "species": [
            {"id": "liveoak", "name": "Live oak", "crown": 12.0, "height": 15.0, "trunk": 3.5, "tau": 0.07, "leaf": "#3f5f3a"},
            {"id": "cedarelm", "name": "Cedar elm", "crown": 10.0, "height": 15.0, "trunk": 3.5, "tau": 0.12, "leaf": "#4a6b40"},
            {"id": "locust", "name": "Honey locust", "crown": 9.0, "height": 13.0, "trunk": 3.0, "tau": 0.26, "leaf": "#5d7a45"},
        ],
        "knownLimits": [
            "Shortwave shading only — no transpiration cooling of air temperature, no wind effects.",
            "Single hour (13:00, Oct 3) — results will differ at other times of day.",
            "Nighttime is not modeled here — canopy modestly raises UTCI overnight by trapping longwave radiation; this tool only shows the daytime cooling benefit.",
            "New trees are shown at mature size — use the ‘years since planting’ slider to see realistic near-term impact.",
            "Endpoint fields (sun/shade) are approximated from each scenario's own existing trees, not a second SOLWEIG run — see the uncertainty layer; accuracy is weakest near building walls.",
            "‘Clean’ and ‘Version 1’ are both real SOLWEIG runs (not one derived from the other) — Version 1 additionally includes the 111 proposed trees from the planting plan.",
        ],
    }
    with open(OUT_DIR / "manifest.json", "w") as f:
        json.dump(manifest, f)
    print(f"Wrote manifest.json ({len(json.dumps(manifest))} bytes)")

    v1_mean = scenarios_out["v1"]["meanUtciBaseline"]
    clean_mean = scenarios_out["clean"]["meanUtciBaseline"]
    print(f"\nScenario comparison: clean={clean_mean} C, v1={v1_mean} C, "
          f"delta={round(clean_mean - v1_mean, 2)} C (proposed trees' domain-average effect)")


if __name__ == "__main__":
    main()
