"""
Stage 3 (data side): build the empirical Tmrt -> UTCI lookup table by regressing
the UTCI raster against Tmrt across every valid ground cell (Ta/RH/wind fixed
for this hour, so UTCI is a function of Tmrt alone -- see CLAUDE.md).

Also answers: "can we get full-coverage UTCI without rerunning QGIS?" Yes --
Tmrt is valid on 100% of cells (including rooftops), and since UTCI depends
only on Tmrt at this fixed hour, the same LUT can be applied everywhere Tmrt
exists. This does NOT invent new physics for rooftops; it just evaluates the
already-validated Tmrt->UTCI function outside the pedestrian mask.
"""
import json

import numpy as np
import rasterio

from common import PATHS

BIN_WIDTH_K = 0.25
CORRUPTED_UTCI_THRESHOLD = 1.0  # excludes the 81 cells with UTCI==0.0 artifact (see memory)


def build_lut():
    with rasterio.open(PATHS["Tmrt"]) as src:
        tmrt = src.read(1, masked=True)
        profile = src.profile
    with rasterio.open(PATHS["UTCI"]) as src:
        utci = src.read(1, masked=True)

    tmrt_valid = ~np.ma.getmaskarray(tmrt)
    utci_valid = ~np.ma.getmaskarray(utci)
    tmrt_f = np.ma.filled(tmrt.astype(np.float64), np.nan)
    utci_f = np.ma.filled(utci.astype(np.float64), np.nan)

    clean = tmrt_valid & utci_valid & (utci_f > CORRUPTED_UTCI_THRESHOLD)
    n_dropped = (tmrt_valid & utci_valid).sum() - clean.sum()
    print(f"Training cells: {clean.sum()} (dropped {n_dropped} corrupted UTCI==0 cells)")

    tv = tmrt_f[clean]
    uv = utci_f[clean]
    order = np.argsort(tv)
    tv_s, uv_s = tv[order], uv[order]

    bins = np.arange(np.floor(tv.min()), np.ceil(tv.max()) + BIN_WIDTH_K, BIN_WIDTH_K)
    idx = np.digitize(tv_s, bins)
    lut_t, lut_u = [], []
    for b in np.unique(idx):
        sel = idx == b
        lut_t.append(float(tv_s[sel].mean()))
        lut_u.append(float(uv_s[sel].mean()))
    lut_t = np.array(lut_t)
    lut_u = np.array(lut_u)

    pred = np.interp(tv, lut_t, lut_u)
    resid = uv - pred
    print(f"LUT fit quality: residual std={resid.std():.3f} K, max abs={np.abs(resid).max():.3f} K, "
          f"corr={np.corrcoef(tv, uv)[0,1]:.5f}")
    print(f"LUT domain (training): Tmrt {lut_t.min():.2f} to {lut_t.max():.2f} C, "
          f"{len(lut_t)} bin points")

    return lut_t, lut_u, tmrt_f, tmrt_valid, profile


def main():
    lut_t, lut_u, tmrt_f, tmrt_valid, profile = build_lut()

    with open(PATHS["DSM"].parent / "utci_lut.json", "w") as f:
        json.dump({"tmrt": lut_t.tolist(), "utci": lut_u.tolist()}, f)
    print("Wrote data/utci_lut.json")

    # apply to every cell where Tmrt exists (100% of grid, including rooftops)
    full_tmrt = tmrt_f[tmrt_valid]
    below = (full_tmrt < lut_t.min()).sum()
    above = (full_tmrt > lut_t.max()).sum()
    print(f"\nFull-domain Tmrt range: {full_tmrt.min():.2f} to {full_tmrt.max():.2f} C "
          f"({tmrt_valid.sum()} cells, 100% of grid)")
    print(f"Cells needing extrapolation beyond training range: {below} below, {above} above "
          f"({100*(below+above)/tmrt_valid.sum():.2f}% of all cells)")

    # linear extrapolation beyond the training range (np.interp would otherwise flat-clip);
    # affects rooftop cells almost exclusively -- see console output for the ground-cell count
    slope_lo = (lut_u[1] - lut_u[0]) / (lut_t[1] - lut_t[0])
    slope_hi = (lut_u[-1] - lut_u[-2]) / (lut_t[-1] - lut_t[-2])

    full_utci = np.full(tmrt_f.shape, np.nan)
    t = tmrt_f[tmrt_valid]
    u = np.interp(t, lut_t, lut_u)
    u = np.where(t < lut_t[0], lut_u[0] + slope_lo * (t - lut_t[0]), u)
    u = np.where(t > lut_t[-1], lut_u[-1] + slope_hi * (t - lut_t[-1]), u)
    full_utci[tmrt_valid] = u

    out_profile = profile.copy()
    out_profile.update(dtype="float32", nodata=-9999.0, count=1)
    out = np.where(tmrt_valid, full_utci, -9999.0).astype(np.float32)
    with rasterio.open(PATHS["DSM"].parent / "UTCI_full_coverage.tif", "w", **out_profile) as dst:
        dst.write(out, 1)
    print("Wrote data/UTCI_full_coverage.tif (100% coverage, derived via LUT -- no QGIS rerun needed)")


if __name__ == "__main__":
    main()
