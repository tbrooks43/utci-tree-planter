# Runtime engine — what happens in the browser on every click

Everything here is `web/main.js`. It loads the two pre-computed endpoint
fields per scenario (`tmrtSun`, `tmrtShade`) plus the real measured baseline
(`tmrtMeasured`) from `data-pipeline.md`'s stage 2/3 output, then does all
further physics live, in milliseconds, with no server involved.

## The core idea: blend, don't re-simulate

For any ground cell, `tmrtSun[k]` and `tmrtShade[k]` are the two extremes —
what that cell's Tmrt would be with zero canopy anywhere, or full canopy
everywhere. Planting or removing trees just changes how far between those two
extremes a given cell sits. The whole runtime is built around computing that
one number — call it `f`, the cell's shaded fraction — and blending.

**Crucially, the blend has to happen in radiant-flux space, not temperature
space.** Tmrt is proportional to the fourth root of absorbed radiant flux, so
averaging Tmrt values directly is physically wrong. The actual blend
(`tmrtAt(k)` in `main.js`):

```js
T4 = (1 - f) * (Tsun + 273.15)^4 + f * (Tshade + 273.15)^4
Tmrt = T4^0.25 - 273.15
```

## Planting a new tree

A crown at height `h`, radius `r`, casts a shadow ellipse on the ground —
centered where the crown's shadow actually falls (not under the trunk),
oriented along the sun's azimuth, semi-minor axis `r`, semi-major axis
`r / sin(altitude)` (a low sun stretches the shadow further). This is
`forEachEllipseCell()` in `main.js`, shared between planting and removal (see
below).

Each new tree multiplies down a per-cell **open-sky fraction** it touches:

```js
openSky[k] *= (1 - coverage * (1 - transmissivity))
```

so overlapping trees compose correctly (two trees each blocking half the sky
never exceed 100% blocked) and each species' real canopy transmissivity
(live oak ~7%, cedar elm ~12%, honey locust ~26%) actually matters — a stand
of honey locusts lets meaningfully more light through than the same footprint
of live oaks. `shadeFrac = 1 - openSky` is the `f` fed into the blend above,
so **new trees blend from the site's real measured baseline toward the fully-
shaded endpoint** — at zero new trees this returns the real measured Tmrt
exactly, not a reconstruction of it.

The "years since planting" slider scales crown radius and height with a linear
growth curve (min 8% of mature size at year 0, full size at year 15) — a
freshly planted tree does almost nothing, by design.

## Removing an existing tree

This is the trickier direction, and worth understanding before trusting the
numbers it produces.

**The problem:** existing trees aren't tracked as discrete shading
contributions the way new trees are — their effect is already baked into
`tmrtMeasured`, the one real number SOLWEIG actually computed for that cell.
There's no "existing tree's marginal contribution" sitting in a variable
anywhere to subtract. And the two endpoint fields (`tmrtSun`/`tmrtShade`)
represent *zero canopy anywhere* / *full canopy everywhere* — global extremes,
not "this one tree's neighborhood minus this one tree."

**The approach:** back out an implied total canopy coverage fraction at each
cell by inverting the same flux blend used everywhere else. Given the real
measured baseline and the two endpoints, solve for the `f` that would have
produced it:

```js
f_base = ((Tbaseline+273.15)^4 - (Tsun+273.15)^4) / ((Tshade+273.15)^4 - (Tsun+273.15)^4)
```

This `f_base` (clamped to [0,1] — see "Why the clamp matters" below) is
`computeOpenSkyBase()`'s output, cached per scenario at load. It's the
existing-conditions analogue of `shadeFrac` above: how shaded a cell already
is, implied by the two real endpoint runs, before any interactive change.

To remove one specific tree, project its own shadow ellipse (same geometry as
planting, using its CDSM-detected `{height, crown}` and the site's real
canopy transmissivity — existing trees have no recorded species, so there's no
per-species value to use) and, for every cell it touches, **divide** that
tree's shading factor back out — the exact inverse of the multiplication new
trees use to shade a cell in:

```js
openSkyAdj[k] = min(1, openSkyBase[k] / (1 - coverage * (1 - tau)))
```

The `min(1, …)` clamp is a physical constraint, not an approximation:
removing shade can cool a cell at most to its fully-sunlit endpoint, never
past it. Multiple overlapping removed trees on the same cell compose the same
way — their factors multiply together before the division, matching
`CLAUDE.md`'s "combining shadows" rule.

**Only cells actually inside a removed tree's footprint are touched.**
Everywhere else, `tmrtBaselineEff` — the baseline the rest of the engine reads
— is the exact, untouched `tmrtMeasured` value. This matters: an earlier
version of this reconstructed *every* cell's baseline from `f_base` via the
flux blend, all the time, even with nothing removed. That's wrong — for cells
where the real measured Tmrt happens to fall slightly outside the
`[tmrtSun, tmrtShade]` envelope (common wherever stage 2's imputed endpoints
are a bit off, which is exactly what the uncertainty layer flags), clamping
`f_base` back into range and reconstructing pulls that cell toward one
endpoint instead of its true measured value — silently shifting the site-wide
mean by more than a degree even when the user hadn't touched anything.
Restricting the reconstruction to only the cells a removal geometrically
touches keeps the "exact at zero intervention" guarantee intact and confines
the approximation to where it's unavoidable.

### Why the clamp matters

`f_base` isn't clamped as an afterthought — it's load-bearing. Stage 2's
endpoint imputation deliberately gives narrow, enclosed cells a *small*
sunlit-minus-shade delta (a tight canyon can't get much darker by adding
canopy on top of what buildings already shade — see `data-pipeline.md`). A
small delta means a small denominator in the `f_base` formula above, and nudge
a small denominator by an entirely ordinary amount of real-world measurement
scatter and the *unclamped* ratio can swing to values like 50 or -30 — nowhere
near a real coverage fraction. Divide a value like that by a removed tree's
shading factor (0.05–1) and the result is even further out of range, and
reconstructing Tmrt at that `f` extrapolates the flux blend wildly past both
endpoints. Clamping `f_base` to `[0, 1]` before it's ever used keeps every
downstream number physically bounded — worst case, a removed tree's own
footprint saturates to its `tmrtSun` endpoint, never to something nonsensical.
The trade-off is a small, bounded bias on the rare out-of-envelope cell, which
is why it's scoped only to cells a removal actually touches rather than
applied everywhere.

### What this means for trusting a removal number

Planting a new tree is close to exact: the physics (ellipse projection, flux
blending, species transmissivity) is the same physics used to build the
endpoint fields in the first place, just applied to one more tree.

Removing an existing tree carries **strictly more uncertainty** than that,
for two independent reasons:

1. It inherits stage 2's endpoint uncertainty (shown by "Show uncertainty" in
   the UI) — the same caveat that already applies to planting near buildings.
2. It additionally assumes each existing tree's local shading factor can be
   cleanly divided back out of whatever total coverage the endpoint model
   implied — reasonable when one tree dominates a cell, weaker in dense grove
   cells where several real trees' canopies overlap and the model has no way
   to attribute the total shade among them individually.

Treat a removal's UTCI delta as directionally right and roughly right, the
same spirit as the rest of this tool's path-(b) approximations — not as
something to put in a memo to the decimal the way a fresh SOLWEIG run's number
would be.

## Combining planting and removal

Both act on the same underlying cell independently and in the expected order:
removal first adjusts what "baseline" means for that cell (closer to
`tmrtSun` than the real measured value), then any *new* tree's `shadeFrac`
blends from that adjusted baseline toward `tmrtShade`, same as it always did.
Plant a tree where you just removed one, and the blend starts from the
removal-adjusted (warmer) baseline, not the original shaded one — which is
the physically correct order of operations.

## UTCI from Tmrt

`makeUtciLookup()` turns each scenario's stage-3 LUT (`tmrt[]`/`utci[]` pairs)
into a dense, evenly-spaced `Float32Array` at load time, so every per-cell
UTCI lookup at paint time is an O(1) linear interpolation — no per-pixel
Bröde-polynomial evaluation, no per-pixel binary search.

## Scenario and removal state

Switching between "Version 1" and "Clean" swaps which scenario's arrays
(`tmrtSun`/`tmrtShade`/`tmrtMeasured`/existing trees/LUT) are active, rebuilds
the existing-tree meshes, and recomputes `tmrtBaselineEff`. **New planted
trees persist across a scenario switch** — they're hypothetical trees on top
of whichever baseline is active. **Removed-tree state does not carry across
scenarios** — it's tracked as a set of `existingTrees` array indices per
scenario, and the two scenarios' existing-tree lists don't correspond
index-for-index (Version 1 has 111 more real trees than Clean), so a removal
in one scenario has no meaningful counterpart in the other.

## Stats panel deltas

The "Mean UTCI" / "Above 38°C" / "Above 46°C" deltas shown in the stats panel
are always relative to the scenario's real, untouched measured baseline
(`computeBaseline()`, computed once per scenario switch) — not relative to
"before your last click." Both planting and removing move these deltas; the
delta only hides itself when neither has happened (`trees.length` is 0 *and*
no existing trees are removed), so a small delta on a freshly loaded scenario
never gets mistaken for a real effect.
