"""
Stage 2: endpoint fields tmrtSun / tmrtShade, via path (b) -- derive from the
existing raster, since no QGIS/UMEP is available locally for two clean SOLWEIG
runs (zero-canopy / full-canopy).

Approach:
  1. Compute sky view factor (SVF) per ground cell from DSM+CDSM (isotropic-sky
     horizon-angle formula: SVF = mean_i[cos^2(horizon_angle_i)] over N azimuth
     directions -- Steyn 1980).
  2. Use the stage-1 shade classification to split ground cells into a sunlit
     population (measured tmrtSun) and a canopy-shadow population (measured
     tmrtShade). Bin both populations by SVF and compute the sunlit-minus-shade
     delta per bin -- open/paved bins get a large delta, low-SVF (narrow/
     enclosed) bins get a small one, per CLAUDE.md.
  3. Impute the missing endpoint per cell:
       - sunlit cell:        tmrtSun = measured;  tmrtShade = tmrtSun  - delta(SVF bin)
       - canopy-shadow cell: tmrtShade = measured; tmrtSun  = tmrtShade + delta(SVF bin)
       - building-shadow cell (neither endpoint observed there): both endpoints
         come from the population bin means directly (no local anchor) --
         these get the largest uncertainty flag.
  4. Report residual spread per bin as the uncertainty proxy, and separately
     report accuracy for the lowest-SVF cells (proxy for narrow-canyon /
     wall-adjacent geometry, the expected worst case per CLAUDE.md). The
     Wall_Height/Wall_Aspect rasters on disk cannot be used for a precise
     south-facing-wall check -- they are statistically identical to each other
     (same min/max/mean/nonzero count), meaning one is mislabeled or duplicated;
     SVF is used as the enclosure proxy instead.
"""
import numpy as np
import rasterio

from common import PATHS, GROUND_VALUE

SUNLIT, BUILDING_SHADOW, CANOPY_SHADOW = 0, 1, 2
N_DIRECTIONS = 16
SVF_SEARCH_RADIUS_M = 60.0
STEP_M = 2.0
N_SVF_BINS = 8


def load_grids():
    with rasterio.open(PATHS["DSM"]) as src:
        dsm = src.read(1).astype(np.float64)
        profile = src.profile
    with rasterio.open(PATHS["DEM"]) as src:
        dem = src.read(1).astype(np.float64)
    with rasterio.open(PATHS["CDSM"]) as src:
        cdsm = np.ma.filled(src.read(1, masked=True).astype(np.float64), 0.0)
        cdsm[cdsm < 0] = 0.0
    with rasterio.open(PATHS["buildings"]) as src:
        ground_mask = src.read(1) == GROUND_VALUE
    with rasterio.open(PATHS["Tmrt"]) as src:
        tmrt = np.ma.filled(src.read(1, masked=True).astype(np.float64), np.nan)
    with rasterio.open(PATHS["DSM"].parent / "shademask.tif") as src:
        shade_class = src.read(1)
    return dsm, dem, cdsm, ground_mask, tmrt, shade_class, profile


def compute_svf(dsm, dem, cdsm, ground_mask):
    rows, cols = dsm.shape
    obstruction = np.maximum(dsm, dem + cdsm)
    target_h = dem

    row_idx, col_idx = np.indices((rows, cols))
    n_steps = int(np.ceil(SVF_SEARCH_RADIUS_M / STEP_M))

    max_angle = np.zeros((rows, cols), dtype=np.float64)  # radians, horizon angle per direction accumulator
    svf_sum = np.zeros((rows, cols), dtype=np.float64)

    for k in range(N_DIRECTIONS):
        az = 2 * np.pi * k / N_DIRECTIONS
        d_col = np.sin(az) / 2.0  # per metre, pixel size 2m
        d_row = -np.cos(az) / 2.0

        horizon = np.zeros((rows, cols), dtype=np.float64)
        r_f = row_idx.astype(np.float64).copy()
        c_f = col_idx.astype(np.float64).copy()

        for step in range(1, n_steps + 1):
            r_f += d_row * STEP_M
            c_f += d_col * STEP_M
            s = step * STEP_M

            r_i = np.round(r_f).astype(np.int64)
            c_i = np.round(c_f).astype(np.int64)
            in_bounds = (r_i >= 0) & (r_i < rows) & (c_i >= 0) & (c_i < cols)
            same_cell = (r_i == row_idx) & (c_i == col_idx)
            active = in_bounds & (~same_cell)
            if not np.any(active):
                if not np.any(in_bounds):
                    break
                continue

            r_c = np.clip(r_i, 0, rows - 1)
            c_c = np.clip(c_i, 0, cols - 1)
            sampled = obstruction[r_c, c_c]
            angle = np.arctan2(np.where(active, sampled - target_h, -np.inf), s)
            horizon = np.where(active, np.maximum(horizon, angle), horizon)

        horizon = np.clip(horizon, 0.0, np.pi / 2)
        svf_sum += np.cos(horizon) ** 2

    svf = svf_sum / N_DIRECTIONS
    return svf


def main():
    dsm, dem, cdsm, ground_mask, tmrt, shade_class, profile = load_grids()

    print(f"Computing SVF ({N_DIRECTIONS} directions, {SVF_SEARCH_RADIUS_M:.0f} m search radius)...")
    svf = compute_svf(dsm, dem, cdsm, ground_mask)
    print(f"  SVF range over ground cells: {svf[ground_mask].min():.3f} to {svf[ground_mask].max():.3f}, "
          f"mean {svf[ground_mask].mean():.3f}")

    sunlit_mask = ground_mask & (shade_class == SUNLIT)
    shade_mask = ground_mask & (shade_class == CANOPY_SHADOW)
    bldg_mask = ground_mask & (shade_class == BUILDING_SHADOW)

    bin_edges = np.quantile(svf[ground_mask], np.linspace(0, 1, N_SVF_BINS + 1))
    bin_edges[0] -= 1e-6
    bin_edges[-1] += 1e-6
    svf_bin = np.digitize(svf, bin_edges) - 1  # 0..N_SVF_BINS-1
    svf_bin = np.clip(svf_bin, 0, N_SVF_BINS - 1)

    bin_delta = np.full(N_SVF_BINS, np.nan)
    bin_resid_std = np.full(N_SVF_BINS, np.nan)
    bin_sunlit_mean = np.full(N_SVF_BINS, np.nan)
    bin_shade_mean = np.full(N_SVF_BINS, np.nan)

    print(f"\n=== SVF-binned sunlit-shade delta ({N_SVF_BINS} quantile bins) ===")
    print(f"{'bin':>4} {'svf_lo':>7} {'svf_hi':>7} {'n_sun':>6} {'n_shd':>6} {'sun_mean':>9} {'shd_mean':>9} {'delta':>7}")
    for b in range(N_SVF_BINS):
        sun_sel = sunlit_mask & (svf_bin == b)
        shd_sel = shade_mask & (svf_bin == b)
        n_sun, n_shd = sun_sel.sum(), shd_sel.sum()
        if n_sun >= 3 and n_shd >= 3:
            sun_mean = tmrt[sun_sel].mean()
            shd_mean = tmrt[shd_sel].mean()
            delta = sun_mean - shd_mean
            bin_delta[b] = delta
            bin_sunlit_mean[b] = sun_mean
            bin_shade_mean[b] = shd_mean
            # residual spread proxy: combined within-population std for this bin
            bin_resid_std[b] = np.sqrt(tmrt[sun_sel].std() ** 2 + tmrt[shd_sel].std() ** 2)
            print(f"{b:4d} {bin_edges[b]:7.3f} {bin_edges[b+1]:7.3f} {n_sun:6d} {n_shd:6d} "
                  f"{sun_mean:9.2f} {shd_mean:9.2f} {delta:7.2f}")
        else:
            print(f"{b:4d} {bin_edges[b]:7.3f} {bin_edges[b+1]:7.3f} {n_sun:6d} {n_shd:6d}  (too few samples)")

    # fill any empty bins by nearest populated bin (delta should vary smoothly with SVF)
    valid_bins = np.where(~np.isnan(bin_delta))[0]
    for b in range(N_SVF_BINS):
        if np.isnan(bin_delta[b]):
            nearest = valid_bins[np.argmin(np.abs(valid_bins - b))]
            bin_delta[b] = bin_delta[nearest]
            bin_resid_std[b] = bin_resid_std[nearest]
            bin_sunlit_mean[b] = bin_sunlit_mean[nearest]
            bin_shade_mean[b] = bin_shade_mean[nearest]

    tmrt_sun = np.full(dsm.shape, np.nan)
    tmrt_shade = np.full(dsm.shape, np.nan)
    uncertainty = np.full(dsm.shape, np.nan)

    tmrt_sun[sunlit_mask] = tmrt[sunlit_mask]
    tmrt_shade[sunlit_mask] = tmrt[sunlit_mask] - bin_delta[svf_bin[sunlit_mask]]
    uncertainty[sunlit_mask] = bin_resid_std[svf_bin[sunlit_mask]]

    tmrt_shade[shade_mask] = tmrt[shade_mask]
    tmrt_sun[shade_mask] = tmrt[shade_mask] + bin_delta[svf_bin[shade_mask]]
    uncertainty[shade_mask] = bin_resid_std[svf_bin[shade_mask]]

    tmrt_sun[bldg_mask] = bin_sunlit_mean[svf_bin[bldg_mask]]
    tmrt_shade[bldg_mask] = bin_shade_mean[svf_bin[bldg_mask]]
    uncertainty[bldg_mask] = 1.5 * bin_resid_std[svf_bin[bldg_mask]]  # fully imputed, no local anchor -> inflate

    print(f"\n=== Imputation summary ===")
    print(f"  sunlit cells (measured tmrtSun, imputed tmrtShade): {sunlit_mask.sum()}")
    print(f"  canopy-shadow cells (measured tmrtShade, imputed tmrtSun): {shade_mask.sum()}")
    print(f"  building-shadow cells (both endpoints fully imputed, highest uncertainty): {bldg_mask.sum()}")

    lowest_svf_thresh = np.quantile(svf[ground_mask], 0.1)
    low_svf_mask = ground_mask & (svf <= lowest_svf_thresh)
    print(f"\n=== Lowest-decile SVF cells (proxy for narrow-canyon / wall-adjacent geometry) ===")
    print(f"  n={low_svf_mask.sum()}, mean uncertainty={uncertainty[low_svf_mask].mean():.2f} K "
          f"vs overall mean uncertainty={uncertainty[ground_mask].mean():.2f} K")
    print(f"  mean delta applied in this band: {bin_delta[svf_bin[low_svf_mask]].mean():.2f} K "
          f"vs overall mean delta: {np.average(bin_delta[svf_bin[ground_mask]]):.2f} K")

    out_profile = profile.copy()
    out_profile.update(dtype="float32", nodata=-9999.0, count=1)
    for name, arr in [("tmrtSun", tmrt_sun), ("tmrtShade", tmrt_shade), ("endpoint_uncertainty", uncertainty)]:
        out = np.where(ground_mask, arr, -9999.0).astype(np.float32)
        with rasterio.open(PATHS["DSM"].parent / f"{name}.tif", "w", **out_profile) as dst:
            dst.write(out, 1)
    print("\nWrote data/tmrtSun.tif, data/tmrtShade.tif, data/endpoint_uncertainty.tif")


if __name__ == "__main__":
    main()
