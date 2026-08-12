"""
Stage 1: shade mask from geometry.

Computes solar position for the design hour, then ray-casts the sun through
DSM (ground+building) and CDSM (canopy height) to classify every ground cell
as sunlit, building-shadowed, or canopy-shadowed. Validated against the Tmrt
raster and cross-checked against SOLWEIG's own Shadow_2020_277_1300D.tif
(used only as a validation target -- the mask itself must be computed here so
the runtime engine can recompute it for hypothetical new tree placements).
"""
import numpy as np
import pandas as pd
import pvlib
import rasterio

from common import (PATHS, LAT, LON, UTC_OFFSET_HOURS, YEAR, DAY_OF_YEAR, HOUR_LOCAL, GROUND_VALUE,
                     load_corrected_canopy_and_buildingtop)

SUNLIT, BUILDING_SHADOW, CANOPY_SHADOW = 0, 1, 2
STEP_M = 2.0  # one pixel, matching the native resolution of the reference SOLWEIG shadow algorithm


def solar_position():
    date = pd.Timestamp(year=YEAR, month=1, day=1) + pd.Timedelta(days=DAY_OF_YEAR - 1, hours=HOUR_LOCAL)
    ts_utc = date.tz_localize(f"Etc/GMT{-UTC_OFFSET_HOURS:+d}").tz_convert("UTC")
    pos = pvlib.solarposition.get_solarposition(pd.DatetimeIndex([ts_utc]), LAT, LON)
    az = float(pos["azimuth"].iloc[0])
    alt = float(pos["apparent_elevation"].iloc[0])
    return az, alt, ts_utc


def load_grids():
    with rasterio.open(PATHS["DSM"]) as src:
        transform = src.transform
        profile = src.profile
    with rasterio.open(PATHS["DEM"]) as src:
        dem = src.read(1).astype(np.float64)
    with rasterio.open(PATHS["buildings"]) as src:
        ground_mask = src.read(1) == GROUND_VALUE
    canopy_height, building_top = load_corrected_canopy_and_buildingtop()
    return building_top, dem, canopy_height, ground_mask, transform, profile


def cast_shadow_mask(building_top, dem, canopy_height, ground_mask, azimuth_deg, altitude_deg):
    rows, cols = building_top.shape
    canopy_top = dem + canopy_height

    az = np.radians(azimuth_deg)
    alt = np.radians(altitude_deg)
    tan_alt = np.tan(alt)

    # direction toward the sun, in pixel-index space (row increases south, col increases east)
    pixel_size = 2.0
    d_col = np.sin(az) / pixel_size  # per metre of horizontal travel
    d_row = -np.cos(az) / pixel_size

    max_height = max(np.nanmax(building_top - dem), np.nanmax(canopy_height)) + 1.0
    max_dist = max_height / max(tan_alt, 1e-6) + STEP_M * 4
    n_steps = int(np.ceil(max_dist / STEP_M))

    row_idx, col_idx = np.indices((rows, cols))
    target_h = dem[row_idx, col_idx]  # eye height reference = bare ground

    result = np.full((rows, cols), SUNLIT, dtype=np.uint8)
    resolved = ~ground_mask  # only need to resolve ground cells; mark rest resolved (ignored)

    r_f = row_idx.astype(np.float64).copy()
    c_f = col_idx.astype(np.float64).copy()

    for step in range(1, n_steps + 1):
        r_f += d_row * STEP_M
        c_f += d_col * STEP_M
        s = step * STEP_M
        ray_height = target_h + s * tan_alt

        r_i = np.round(r_f).astype(np.int64)
        c_i = np.round(c_f).astype(np.int64)
        in_bounds = (r_i >= 0) & (r_i < rows) & (c_i >= 0) & (c_i < cols)
        same_cell = (r_i == row_idx) & (c_i == col_idx)  # a cell cannot shadow itself

        still_active = (~resolved) & in_bounds & (~same_cell)
        if not np.any(still_active):
            if not np.any(in_bounds):
                break
            continue

        r_c = np.clip(r_i, 0, rows - 1)
        c_c = np.clip(c_i, 0, cols - 1)

        sampled_building = building_top[r_c, c_c]
        sampled_canopy = canopy_top[r_c, c_c]

        blocked_building = still_active & (sampled_building > ray_height)
        blocked_canopy = still_active & (~blocked_building) & (sampled_canopy > ray_height)

        result[blocked_building] = BUILDING_SHADOW
        result[blocked_canopy] = CANOPY_SHADOW
        resolved |= blocked_building | blocked_canopy

        if np.all(resolved):
            break

    result[~ground_mask] = SUNLIT  # placeholder; caller masks these out of reporting
    return result, n_steps


def main():
    az, alt, ts_utc = solar_position()
    print(f"Solar position at {YEAR}-day{DAY_OF_YEAR} {HOUR_LOCAL}:00 local (UTC {ts_utc}):")
    print(f"  azimuth  = {az:.2f} deg")
    print(f"  altitude = {alt:.2f} deg")

    building_top, dem, canopy_height, ground_mask, transform, profile = load_grids()
    mask, n_steps = cast_shadow_mask(building_top, dem, canopy_height, ground_mask, az, alt)
    print(f"  ray-march steps used: {n_steps} (max {n_steps * STEP_M:.1f} m)")

    with rasterio.open(PATHS["Tmrt"]) as src:
        tmrt = src.read(1, masked=True)
    tmrt = np.ma.filled(tmrt.astype(np.float64), np.nan)

    print("\n=== Classification counts (ground cells only) ===")
    for name, val in [("sunlit", SUNLIT), ("building_shadow", BUILDING_SHADOW), ("canopy_shadow", CANOPY_SHADOW)]:
        sel = ground_mask & (mask == val)
        n = sel.sum()
        t = tmrt[sel]
        t = t[~np.isnan(t)]
        print(f"  {name:16s} n={n:6d}  Tmrt mean={t.mean():.2f}  median={np.median(t):.2f}  std={t.std():.2f}" if len(t) else f"  {name:16s} n={n:6d}  (no valid Tmrt)")

    print("\n=== Cross-check vs SOLWEIG Shadow_2020_277_1300D.tif ===")
    with rasterio.open(PATHS["Shadow_reference"]) as src:
        ref = src.read(1, masked=True)
    ref = np.ma.filled(ref.astype(np.float64), np.nan)
    # reference convention: 1.0 = sunlit, ~0.05 = canopy shadow (matches tree transmissivity), 0.0 = building shadow
    ref_class = np.full(ref.shape, -1, dtype=np.int8)
    ref_class[np.isclose(ref, 1.0)] = SUNLIT
    ref_class[np.isclose(ref, 0.0)] = BUILDING_SHADOW
    ref_class[(~np.isclose(ref, 0.0)) & (~np.isclose(ref, 1.0)) & (~np.isnan(ref))] = CANOPY_SHADOW

    sel = ground_mask & (ref_class >= 0)
    agree = (mask[sel] == ref_class[sel])
    print(f"  overall agreement: {agree.mean()*100:.1f}% over {sel.sum()} ground cells")
    for name, val in [("sunlit", SUNLIT), ("building_shadow", BUILDING_SHADOW), ("canopy_shadow", CANOPY_SHADOW)]:
        ref_sel = sel & (ref_class == val)
        if ref_sel.sum() == 0:
            continue
        recall = (mask[ref_sel] == val).mean()
        print(f"  reference={name:16s} n={ref_sel.sum():6d}  recall(matched)={recall*100:.1f}%")

    out_profile = profile.copy()
    out_profile.update(dtype="uint8", nodata=255, count=1)
    out = np.where(ground_mask, mask, 255).astype(np.uint8)
    with rasterio.open(PATHS["DSM"].parent / "shademask.tif", "w", **out_profile) as dst:
        dst.write(out, 1)
    print("\nWrote data/shademask.tif (0=sunlit, 1=building_shadow, 2=canopy_shadow, 255=non-ground)")


if __name__ == "__main__":
    main()
