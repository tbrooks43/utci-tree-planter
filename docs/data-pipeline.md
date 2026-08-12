# Data pipeline — from SOLWEIG rasters to what the browser loads

Everything here is offline Python (`scripts/`), run once per scenario ahead of
time. Nothing in this document runs when someone uses the tool — it's how the
`web/data/` files the browser fetches actually got made.

## Inputs

One SOLWEIG/UMEP run per scenario produces, on the same 2 m grid:

| File | Contents |
|---|---|
| `DSM.tif` | Ground + building elevation |
| `DEM.tif` | Bare ground elevation |
| `CDSM.tif` | Canopy height above ground |
| `Tmrt.tif` | Mean radiant temperature at 13:00, Oct 3 2020 |
| `UTCI.tif` | UTCI at the same hour |

Plus scalars from the met forcing file (`scripts/common.py`): air temp
26.71 °C, RH 48.44%, wind 2.38 m/s, sun at 54° altitude / 197° azimuth. Site
constants (lat/lon, UTC offset, tree transmissivity default 5%) live there too.

`buildings.tif` (1 = ground/pedestrian cell, 0 = rooftop) and `landcover.tif`
are auxiliary rasters used to correct SOLWEIG's own inputs before the real run
— see "Corrections before the SOLWEIG rerun" below.

## Rerun order (if source data changes)

1. `scripts/build_landcover.py` — reclassifies the real Austin 2017 LULC
   raster into UMEP's land-cover codes (paved / building / grass / bare soil /
   water), used to fix SOLWEIG's own building detection (see below).
2. `scripts/build_corrected_cdsm.py` (or the `_clean_` variants for the Clean
   scenario) — infers a denser canopy height field from DSM-DEM elevation
   bumps SOLWEIG's raw CDSM missed, restricted to land-cover classes where
   vegetation is plausible (never paved or water — see the docstring in
   `common.load_corrected_canopy_and_buildingtop` for the highway-mislabeled-
   as-canopy bug this guards against).
3. QGIS/UMEP headless rerun (`qgis_process`): Wall Height/Aspect → Sky View
   Factor → SOLWEIG → Spatial Thermal Comfort, using the corrected building
   mask and CDSM from steps 1–2.
4. `scripts/stage1_shademask.py` → `scripts/stage2_endpoints.py` →
   `scripts/build_utci_lut.py` — described below. Run once per scenario via
   the `UTCI_SCENARIO=clean` environment variable for the Clean scenario
   (`scripts/common.py` resolves `DATA_DIR` to `data_clean/` when it's set).
5. `scripts/export_for_web.py` — bundles both scenarios into
   `web/data/manifest.json` + flat binary buffers.
6. `scripts/fetch_basemap.py` — only needs rerunning if the site extent
   changes; fetches Esri World Imagery for the "aerial imagery" slider.

## Corrections before the SOLWEIG rerun

SOLWEIG's own DSM-minus-DEM threshold for detecting buildings over-classified
the site (65.4% "building" vs. the real building-vector footprint's 22.0%).
`build_landcover.py` swaps in a real Austin land-cover raster instead — its
building percentage (21.6%) matches the true footprint almost exactly.

Real CDSM only has recorded canopy height on 11.4% of cells (LiDAR gaps).
`build_corrected_cdsm.py` infers canopy on the rest wherever there's an
unexplained DSM-DEM elevation bump *and* the land cover there is grass or bare
soil — never paved or water, which is what previously caused a highway
overpass to get modeled as transmissive shaded parkland instead of full-sun
pavement.

## Stage 1 — shade mask from geometry (`stage1_shademask.py`)

Computes solar position for the design hour via `pvlib`, then ray-marches from
every ground cell toward the sun through the (corrected) DSM and CDSM,
classifying each cell **sunlit**, **building-shadowed**, or
**canopy-shadowed**. This mask is *computed*, not read from SOLWEIG's own
shadow raster, because stage 2 needs the same logic the browser will
eventually reuse for hypothetical new plantings — but it's cross-checked
against SOLWEIG's own `Shadow_*.tif` for sanity (currently ~64% agreement;
tracked as a known secondary gap, doesn't affect the Tmrt/UTCI values
themselves since those come straight from SOLWEIG).

## Stage 2 — endpoint fields (`stage2_endpoints.py`)

The browser's runtime blend (see `runtime-engine.md`) needs two Tmrt fields per
cell — `tmrtSun` (as if nothing shaded it) and `tmrtShade` (as if fully
canopy-shaded). The spec's preferred way to get these is two literal SOLWEIG
runs (zero canopy / uniform canopy); lacking that locally, this stage derives
them from the one real run instead:

1. Compute sky view factor (SVF) per ground cell from DSM+CDSM (16-direction
   horizon-angle method, Steyn 1980).
2. Use stage 1's classification to split ground cells into a **sunlit**
   population (real `tmrtSun`, unknown `tmrtShade`) and a **canopy-shadow**
   population (real `tmrtShade`, unknown `tmrtSun`). Bin both by SVF (8
   quantile bins) and take the sunlit-minus-shade delta per bin — open/paved
   bins get a large delta, narrow/enclosed bins get a small one, matching the
   physical intuition that a tight canyon can't get much darker by adding
   canopy on top of what's already shaded by buildings.
3. Impute the missing endpoint per cell using its bin's delta. Building-shadow
   cells (neither endpoint locally measured) get both endpoints from the bin
   means directly — flagged with the highest uncertainty.
4. The residual spread within each bin becomes `endpoint_uncertainty.tif` —
   this is exactly what the in-app "Show uncertainty" toggle displays, and
   it's the reason `runtime-engine.md`'s tree-removal math has to be so
   careful about cells where the two endpoints sit close together.

This is explicitly an approximation (path "b" in `CLAUDE.md`'s terms), weakest
near south-facing walls where reflected shortwave dominates and there's no
real canopy elsewhere in the scene to analogize from.

## Stage 3 — UTCI lookup table (`build_utci_lut.py`)

At a single hour with Ta/RH/wind fixed, UTCI is a pure function of Tmrt — no
need to reimplement the Bröde polynomial. This stage regresses the real
`UTCI.tif` against the real `Tmrt.tif` across every valid ground cell (0.25 K
bins, excluding 81 cells with a `UTCI == 0.0` raster artifact), producing a
`tmrt[] / utci[]` lookup table. The fit is essentially exact (this is measured
data regressed against itself, not a model) — the LUT's tightness is also used
as a data-integrity check: visible scatter would mean the two rasters weren't
actually the same hour, or wind wasn't spatially uniform.

The same LUT is then applied to every cell with a valid Tmrt, including
rooftops (100% grid coverage) — this doesn't invent new physics for rooftops,
it just evaluates the already-validated Tmrt→UTCI function outside the
pedestrian mask, with linear extrapolation past the training range.

## Export (`export_for_web.py`)

Bundles both scenarios' `tmrtSun` / `tmrtShade` / `tmrtMeasured` (the real
`Tmrt.tif`) / `endpoint_uncertainty` / UTCI LUT into flat `Float32Array`
buffers, plus real building footprints (extruded from `Building_Vector.fgb`)
and existing-tree markers. Existing trees are detected by **local-maxima peak
detection** in the CDSM (not connected-component blobs, which would merge an
entire grove into one absurd "tree") — each gets a `{cx, cz, crown, height}`
record, restricted to ground cells. This is the same list the runtime's
tree-removal feature (`runtime-engine.md`) lets you click on.

This site is small enough (204×317 cells) that the full-Austin PNG-tiling
scheme in `CLAUDE.md` is skipped — plain `Float32Array` buffers are simpler and
plenty fast at this scale.
