# UTCI Tree Planter

Interactive tool for urban heat decision makers in Austin, TX. A user clicks to
place trees on a map, sees the UTCI change immediately, and can walk the site in
first person to experience the result. Single design hour for now.

## Why this exists

Decision makers choose planting locations. They need to see the thermal
consequence of a placement fast enough to argue with it, and they need a number
they can put in a memo. A full SOLWEIG rerun takes minutes to hours; this tool
approximates it in milliseconds so the loop is interactive.

## Data in `data/`

Five rasters, all from one SOLWEIG/UMEP run, all on the same grid and CRS:

| File | Contents |
|---|---|
| DSM | Ground + building elevation |
| DEM | Bare ground elevation |
| CDSM | Canopy height above ground, existing trees |
| Tmrt | Mean radiant temperature, the design hour |
| UTCI | Universal Thermal Climate Index, same hour |

Three scalars needed alongside them, from the met forcing file:
timestamp of the hour, air temperature (Ta), relative humidity (RH), wind speed.

**Inspect before coding.** Report grid shape, cell size, CRS, nodata value, and
min/max for each raster. Confirm all five align cell-for-cell. Confirm Tmrt and
UTCI are the same hour (see LUT section — the check is built in).

## Architecture

Three stages. Stage 1 and 2 are offline Python, stage 3 is the browser.

### 1. Shade mask from geometry

Compute solar position from the timestamp and site coordinates (pvlib, or the
UMEP solar routines). Ray-cast the sun through DSM and CDSM to classify every
ground cell as **sunlit**, **building-shadowed**, or **canopy-shadowed**.

This mask is computed, not read from a file. It must be validated against the
Tmrt raster before anything downstream is trusted: canopy-shadowed cells should
show markedly lower Tmrt than sunlit cells. If they don't separate cleanly, the
solar position or the CDSM registration is wrong. Stop and fix it there.

### 2. Endpoint fields

The runtime engine interpolates between two fields per cell:

- `tmrtSun` — Tmrt with no canopy anywhere
- `tmrtShade` — Tmrt with full canopy everywhere

Two ways to get these, in order of preference:

**(a) Two SOLWEIG runs.** Zero the CDSM for run A; set it to a uniform ~9 m
canopy with 3 m trunk for run B. Buildings stay in both. This is exact and
should be done if QGIS/UMEP is available locally.

**(b) Derive from the existing raster.** The existing trees are measured
shaded-endpoint samples. Use the stage-1 mask to split the Tmrt raster into
sunlit and canopy-shadowed populations, then impute the missing endpoint for
each cell. Stratify the sunlit-to-shaded delta by sky view factor (computable
from DSM+CDSM) and by local Tmrt level, so paved open cells get a large delta
and narrow-canyon cells get a small one.

Path (b) is an approximation. Report its residual spread and surface that
uncertainty in the UI. It is weakest against south-facing walls, where reflected
shortwave dominates and canopy elsewhere in the scene is a poor analogue.

### 3. UTCI lookup table

**Do not reimplement the Bröde polynomial from memory.**

With Ta, RH and wind fixed at one hour, UTCI is a function of Tmrt alone. Build
the table empirically by regressing the UTCI raster against the Tmrt raster
across every valid cell. This is exact for these conditions and needs no
library.

This doubles as a data integrity check: the scatter must be a tight monotonic
curve. Visible spread means the two rasters are not from the same hour, or the
run used spatially varying wind (URock/SpatialTC). Investigate before
proceeding.

If `pythermalcomfort` is installed, cross-check the empirical curve against
`utci(tdb=Ta, tr=t, v=wind, rh=RH)` swept over the Tmrt range. They should agree.

## Runtime physics

**Blend in flux space, not temperature space.** Tmrt is the fourth root of
absorbed radiant flux. Averaging Tmrt values directly is wrong:

```
T4_blend = (1 - f) * (Tsun + 273.15)^4 + f * (Tshade + 273.15)^4
Tmrt     = T4_blend^0.25 - 273.15
f        = crown_coverage * (1 - transmissivity)
```

**Shadow projection.** A crown at height `h` with radius `r` projects to an
ellipse centred at `(x + h/tan(alt) * shx, z + h/tan(alt) * shz)`, semi-minor
`r`, semi-major `r / sin(alt)`, oriented along the shadow axis. `shx = -sin(azi)`,
`shz = cos(azi)`. Use crown *centre* height, not total tree height.

**Combining shadows.** Track per-cell open-sky fraction and multiply
contributions: `openSky *= (1 - coverage * (1 - tau))`. Overlapping canopy and
building shadow then never push total coverage past 1. Shade fraction is
`1 - openSky`.

**Species transmissivity matters and is often the actual decision.** SOLWEIG
defaults to 3%. Real values by species: live oak ~0.07, cedar elm ~0.12, honey
locust ~0.26. Expose it in the UI.

## Tiling for the browser

Full Austin at 2 m is ~200M cells — cannot ship to a browser. Tile at 1 km
(500x500 cells at 2 m). Quantise Tmrt to uint16 and store as PNG so the GPU
uploads it directly as a texture with no decode step. Lazy-load on camera move.

Repaint only the cells a planting actually touched, not the whole grid. The
tree-application function should return its touched-cell list, which also gives
exact undo.

## Frontend

three.js. Plan view and walk view are the same scene with different cameras —
don't build a separate 2D map path. InstancedMesh for trees. Ground is a
DataTexture updated in place.

Metrics that matter to the audience, beyond the map: percent of ground above the
strong (38 °C) and extreme (46 °C) UTCI thresholds, new shaded area in m²,
shaded-route coverage to bus stops and schools, population-weighted exposure
reduction, cost per unit cooling.

## Validation loop

This is the acceptance test, not an optional extra.

1. Plant 3–4 scenarios in the tool: a lone street tree, a dense grove, trees
   against a south-facing wall, trees in an already-shaded canyon.
2. Export positions, write them into a copy of the CDSM.
3. Recompute SVF, rerun SOLWEIG, difference Tmrt against baseline.
4. Compare to the browser engine's prediction.

Target: within ~1 K UTCI on shaded cells. The wall-adjacent case is the expected
worst performer. If it fails badly, either add a third endpoint run or flag that
geometry in the UI rather than quietly reporting a wrong number.

## Known limits — keep these visible in the UI

- **Shortwave shading only.** No transpiration cooling of air temperature, no
  wind effects. This is most of the daytime effect but not all of it.
- **Single hour.** The case for a planting location shifts with sun azimuth
  across the day. A tool that only knows 15:00 will give confidently wrong
  advice about a morning bus stop. Multi-hour is the first real extension.
- **Nighttime reverses sign.** Canopy reduces sky view factor and traps
  longwave, modestly *raising* UTCI overnight. Overnight heat matters for
  mortality. Do not build a tool that can only ever show trees helping — it
  will lose credibility with anyone technical.
- **Trees start as sticks.** A newly planted tree delivers almost nothing for
  years. A "years from planting" slider scaling crown radius is worth adding,
  because the gap between a planting budget and its benefit is a genuine
  decision variable.

## Related prior art

UMEP's **TreePlanter** solves the inverse problem — hill-climbing optimisation
of tree positions against Tmrt. Worth reading before building; its
shadow-to-Tmrt logic is essentially what gets ported to the browser. It also
complements this tool: let TreePlanter propose an optimum, let this tool be
where a decision maker argues with it.

- TreePlanter docs: https://umep-docs.readthedocs.io/en/latest/post_processor/Outdoor%20Thermal%20Comfort%20TreePlanter.html
- TreePlanter paper (GMD 15, 1107, 2022): https://gmd.copernicus.org/articles/15/1107/2022/

## Working notes

- A prototype with synthetic data exists and demonstrates the intended
  interaction: `utci-tree-planter.html`. Its physics (ellipse projection,
  multiplicative shading, flux-space blending) is sound. Its input fields and
  UTCI curve are placeholders and must not be carried over.
- Never let a number reach the UI that came from a placeholder curve.
