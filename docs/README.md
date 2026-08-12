# UTCI Tree Planter — how it works

This is the as-built companion to the top-level `CLAUDE.md` spec: it documents
what's actually implemented for Jamestown Park, Austin, with real file and
function names, not the target architecture. Read this if you're picking the
project back up, extending it, or just want to know where a number on screen
came from.

Three documents:

- **This file** — what the tool is, how the pieces fit together, how to run it.
- [`data-pipeline.md`](data-pipeline.md) — the offline Python stages that turn
  five SOLWEIG rasters into the two fields the browser actually uses, and how
  UTCI gets computed from Tmrt.
- [`runtime-engine.md`](runtime-engine.md) — the physics that runs live in the
  browser every time you plant or remove a tree, including how tree *removal*
  works, which is the trickier of the two.

## What it is

A single-page three.js app (`web/index.html` + `web/main.js`) for one site
(Jamestown Park) and one design hour (13:00, Oct 3 2020). Click the ground to
plant a tree, click an existing tree to remove it, and the UTCI heat-stress map
repaints immediately — no server round trip, no re-running SOLWEIG. Every
number the tool shows is derived from real SOLWEIG output; nothing on screen
comes from a placeholder curve.

## Why two stages instead of one

A full SOLWEIG run takes minutes to hours per scenario, and a park visit or
council meeting needs an answer in the time it takes to click. So the heavy
physics is split:

- **Offline (Python, `scripts/`)**: run SOLWEIG (or an approximation of it)
  *twice* per scenario — once as if there were no canopy anywhere, once as if
  the whole scene were fully shaded — to get two Tmrt fields, `tmrtSun` and
  `tmrtShade`. This is the expensive part, done once, not per click.
- **Online (browser, `web/main.js`)**: for any hypothetical planting or
  removal, blend between those two pre-computed fields cell-by-cell, weighted
  by how much new/removed canopy actually covers each cell. Blending is cheap
  — milliseconds for the whole grid — so it can run on every click, every
  frame of the "years since planting" slider, every scenario toggle.

The blend itself isn't linear averaging — see `runtime-engine.md` for why it
has to happen in radiant-flux space, not temperature space.

## The two scenarios

The tool ships two independent, real SOLWEIG runs for the same site and hour,
toggleable in the UI:

| Scenario | What it represents | Data folder |
|---|---|---|
| **Version 1** (default) | The park's actual planting *plan* — 111 proposed trees already standing | `data/` |
| **Clean** | Existing conditions only — those 111 trees digitally removed before the SOLWEIG rerun | `data_clean/` |

Neither is derived from the other by approximation; both went through the full
offline pipeline independently (see `data-pipeline.md`). Any tree you plant or
remove interactively in the UI is layered on top of whichever scenario is
active, and is re-evaluated against that scenario's own baseline if you switch.

## Directory map

```
data/, data_clean/     SOLWEIG output + derived stage-1/2/3 products, per scenario
rerun/                 intermediate QGIS/UMEP rerun artifacts (wall H/A, SVF, SOLWEIG runs)
scripts/               offline Python pipeline (see data-pipeline.md)
web/                   the actual browser app
  index.html             UI shell, panel layout
  main.js                scene setup, physics, interaction (see runtime-engine.md)
  data/                  manifest.json + flat binary rasters exported for the browser
docs/                   this folder
CLAUDE.md               target architecture / spec this build follows
```

## Running it locally

```bash
python -m http.server 8123 --directory web
```

then open `http://localhost:8123`. The browser aggressively caches `main.js`
and the fetched data files — if you edit `main.js`, bump the `?v=N` query on
the `<script>` tag in `index.html`, and load the page with a `?cb=N` query too,
or your edits won't show up even after a hard reload.

If the underlying raster data changes, rerun the offline pipeline in order —
see the "Rerun order" section of `data-pipeline.md` — then
`scripts/export_for_web.py` to regenerate `web/data/`.

## Known limitations (also shown in-app)

- **Shortwave shading only.** No transpiration cooling, no wind effects.
- **Single hour.** A tree that helps a bus stop at 13:00 might do nothing for
  it at 08:00. Not modeled.
- **Daytime-only sign.** Canopy traps longwave overnight and modestly *raises*
  UTCI after dark. The tool only ever shows the daytime cooling benefit —
  don't read "trees always help" out of it.
- **New trees render at chosen growth stage, not instantly mature** — the
  "years since planting" slider scales crown size; a freshly planted tree
  starts near-zero effect.
- **Endpoint fields are approximated, not double-run**, for both scenarios —
  see `data-pipeline.md`'s stage 2 and the in-app uncertainty overlay.
- **Existing-tree removal is a further approximation on top of that** — see
  `runtime-engine.md`'s "Removing an existing tree" section for exactly what
  it assumes and where it's weakest.
