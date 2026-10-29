/* ============================================================================
   UTCI Tree Planter — Jamestown Park, real SOLWEIG-derived data.
   Loads manifest.json + binary raster buffers exported by scripts/export_for_web.py.
   Physics (flux-space blend, ellipse shadow projection, multiplicative shade
   combination) mirrors the validated prototype and CLAUDE.md's runtime spec.
   ========================================================================= */

const DATA_DIR = 'data/';

const BANDS = [
  { max:26, label:'No thermal stress',   rgb:[ 92,142,168] },
  { max:32, label:'Moderate heat stress',rgb:[214,196,120] },
  { max:38, label:'Strong heat stress',  rgb:[224,150, 72] },
  { max:46, label:'Very strong heat stress', rgb:[203, 84, 58] },
  { max:99, label:'Extreme heat stress', rgb:[140, 38, 52] },
];
function bandFor(u){ for(const b of BANDS) if(u < b.max) return b; return BANDS[4]; }

const UNCERTAINTY_RAMP = [
  [0,   [58, 92,120]],
  [4,   [79,128,150]],
  [8,   [150,150, 90]],
  [14,  [200,120, 60]],
  [30,  [190, 60, 60]],
];
function uncertaintyColor(u){
  for (let i=1;i<UNCERTAINTY_RAMP.length;i++){
    const [t0,c0]=UNCERTAINTY_RAMP[i-1], [t1,c1]=UNCERTAINTY_RAMP[i];
    if (u <= t1) {
      const f = (u-t0)/(t1-t0);
      return c0.map((v,k)=>v+(c1[k]-v)*f);
    }
  }
  return UNCERTAINTY_RAMP[UNCERTAINTY_RAMP.length-1][1];
}

// Builds a fast O(1) UTCI(Tmrt) lookup closure from a scenario's own empirical
// LUT (tmrt[]/utci[] pairs from stage 3's regression). Each scenario gets its
// own -- the relationship is nearly identical between scenarios (same site,
// hour, met conditions) but built independently for correctness.
function makeUtciLookup(lut){
  const lutT = lut.tmrt, lutU = lut.utci;
  const LUT_MIN = lutT[0], LUT_MAX = lutT[lutT.length - 1], LUT_STEP = 0.1;
  const slopeLo = (lutU[1] - lutU[0]) / (lutT[1] - lutT[0]);
  const slopeHi = (lutU[lutU.length-1] - lutU[lutU.length-2]) / (lutT[lutT.length-1] - lutT[lutT.length-2]);
  const UTCI_LUT = (() => {
    const out = [];
    let j = 0;
    for (let t = LUT_MIN; t <= LUT_MAX + 1e-9; t += LUT_STEP) {
      while (j < lutT.length - 2 && lutT[j+1] < t) j++;
      const f = (t - lutT[j]) / (lutT[j+1] - lutT[j]);
      out.push(lutU[j] * (1 - f) + lutU[j+1] * f);
    }
    return Float32Array.from(out);
  })();
  return function utciFromTmrt(t){
    if (t < LUT_MIN) return lutU[0] + slopeLo * (t - LUT_MIN);
    if (t > LUT_MAX) return lutU[lutU.length-1] + slopeHi * (t - LUT_MAX);
    const x = (t - LUT_MIN) / LUT_STEP;
    const i = Math.min(UTCI_LUT.length - 2, Math.floor(x)), f = x - i;
    return UTCI_LUT[i] * (1 - f) + UTCI_LUT[i + 1] * f;
  };
}

async function main(){
  const noCache = { cache: 'no-store' };
  const manifest = await fetch(DATA_DIR + 'manifest.json', noCache).then(r => r.json());
  const scenarioNames = Object.keys(manifest.scenarios);
  const bufPromises = { groundMask: fetch(DATA_DIR + 'groundMask.bin', noCache).then(r => r.arrayBuffer()) };
  for (const name of scenarioNames) {
    bufPromises['sun_'+name] = fetch(DATA_DIR + `tmrtSun_${name}.bin`, noCache).then(r => r.arrayBuffer());
    bufPromises['shade_'+name] = fetch(DATA_DIR + `tmrtShade_${name}.bin`, noCache).then(r => r.arrayBuffer());
    bufPromises['measured_'+name] = fetch(DATA_DIR + `tmrtMeasured_${name}.bin`, noCache).then(r => r.arrayBuffer());
    bufPromises['unc_'+name] = fetch(DATA_DIR + `uncertainty_${name}.bin`, noCache).then(r => r.arrayBuffer());
  }
  const bufKeys = Object.keys(bufPromises);
  const bufs = Object.fromEntries(bufKeys.map((k,i) => [k, null]));
  await Promise.all(bufKeys.map(async k => { bufs[k] = await bufPromises[k]; }));

  const ROWS = manifest.grid.rows, COLS = manifest.grid.cols, CELL = manifest.grid.cellSize;
  const NC = ROWS * COLS;
  const WIDTH = COLS * CELL, HEIGHT = ROWS * CELL;

  const cellIsGround = new Uint8Array(bufs.groundMask);

  // tmrtSun/tmrtShade are the stage-2 endpoint fields (building shadow already
  // baked in, per CLAUDE.md "buildings stay in both" runs). tmrtBaseline is the
  // real SOLWEIG-measured Tmrt -- used directly whenever nothing new has been
  // planted, so the tool reproduces ground truth exactly at zero trees. Only
  // NEW planted trees perturb the scene, blending from tmrtBaseline toward
  // tmrtShade by their own marginal coverage. Building geometry is NOT used to
  // compute shading here (that would double-count shadow already baked into
  // the endpoint fields) -- buildings are visual/collision only.
  //
  // Two independent SOLWEIG-derived scenarios: "v1" (proposed planting, the
  // default) and "clean" (existing conditions, 111 proposed trees removed).
  // Both are real runs, not one derived from the other. New user-planted trees
  // persist across a scenario switch -- they represent hypothetical additional
  // plantings on top of whichever baseline is active.
  const scenarioData = {};
  for (const name of scenarioNames) {
    scenarioData[name] = {
      tmrtSun: new Float32Array(bufs['sun_'+name]),
      tmrtShade: new Float32Array(bufs['shade_'+name]),
      tmrtBaseline: new Float32Array(bufs['measured_'+name]),
      uncertainty: new Float32Array(bufs['unc_'+name]),
      utciFromTmrt: makeUtciLookup(manifest.scenarios[name].utciLut),
      existingTrees: manifest.scenarios[name].existingTrees,
      label: manifest.scenarios[name].label,
      meanUtciBaseline: manifest.scenarios[name].meanUtciBaseline,
    };
  }
  // Implied existing-canopy openSky fraction at each cell, backed out from the
  // scenario's own two SOLWEIG endpoint runs by solving the same flux-blend
  // used at runtime (T^4 = (1-f)*Tsun^4 + f*Tshade^4) for f given the real
  // measured baseline. This is what lets "remove an existing tree" undo a
  // share of that implied coverage using the same multiplicative-shadow math
  // as planting, instead of needing a third SOLWEIG run per removed tree.
  function computeOpenSkyBase(name){
    const S = scenarioData[name];
    const out = new Float32Array(NC);
    for (let k = 0; k < NC; k++) {
      if (!cellIsGround[k]) { out[k] = 1; continue; }
      const tsun4 = Math.pow(S.tmrtSun[k] + 273.15, 4);
      const tshade4 = Math.pow(S.tmrtShade[k] + 273.15, 4);
      const tbase4 = Math.pow(S.tmrtBaseline[k] + 273.15, 4);
      const denom = tshade4 - tsun4;
      const f = denom > 1e-6 ? (tbase4 - tsun4) / denom : 0;
      // Clamped to [0,1] -- narrow/enclosed cells get a small SVF-bin delta
      // by the stage-2 imputation's own design (CLAUDE.md's stratification),
      // so denom can be small there and an unclamped f swings wildly for a
      // perfectly ordinary measured baseline. Clamping keeps this bounded;
      // the resulting bias in the (rare) out-of-envelope case is imperceptible
      // in practice and confined to cells actually touched by a removal --
      // see recomputeBaselineEff, which leaves every other cell untouched.
      out[k] = 1 - Math.min(1, Math.max(0, f));
    }
    return out;
  }
  for (const name of scenarioNames) scenarioData[name].openSkyBase = computeOpenSkyBase(name);

  let scenarioName = manifest.defaultScenario;
  let SCEN = scenarioData[scenarioName];

  function idx(i, j){ return j * COLS + i; }
  function cellCentre(i, j){
    return [ (i + 0.5) * CELL - WIDTH / 2, (j + 0.5) * CELL - HEIGHT / 2 ];
  }

  const SPECIES = manifest.species;
  const BUILDINGS = manifest.buildings;
  const MAX_EXISTING_TREES = Math.max(...scenarioNames.map(n => scenarioData[n].existingTrees.length));

  const altRad = manifest.site.sunAltitudeDeg * Math.PI / 180;
  const aziRad = manifest.site.sunAzimuthDeg * Math.PI / 180;
  const SUN_ALT_SIN = Math.sin(altRad), SUN_ALT_TAN = Math.tan(altRad);
  const SHX = -Math.sin(aziRad), SHZ = Math.cos(aziRad); // shadow-fall direction, away from sun

  let openSky = new Float32Array(NC);   // 1 = no NEW tree shades this cell yet
  let shadeFrac = new Float32Array(NC); // marginal coverage added by NEW trees only
  const trees = []; // {x, z, sp}

  // years-since-planting growth model: linear ramp to full size over 15 years
  function growthFraction(years){ return Math.max(0.08, Math.min(1, years / 15)); }

  // Casts a crown's shadow ellipse (CLAUDE.md's projection formula) and calls
  // cb(cellIndex, coverageFraction) for every ground cell it touches. Shared
  // by new-tree planting (multiplies a cell's openSky down) and existing-tree
  // removal (divides a cell's implied openSky back up) -- same geometry, used
  // in opposite directions.
  function forEachEllipseCell(treeX, treeZ, crownCentreHeight, r, cb){
    const L = crownCentreHeight / SUN_ALT_TAN;
    const cx = treeX + L * SHX, cz = treeZ + L * SHZ;
    const semiMajor = r / SUN_ALT_SIN;
    const reach = semiMajor + CELL;

    const i0=Math.max(0,Math.floor((cx-reach+WIDTH/2)/CELL)), i1=Math.min(COLS-1,Math.ceil((cx+reach+WIDTH/2)/CELL));
    const j0=Math.max(0,Math.floor((cz-reach+HEIGHT/2)/CELL)), j1=Math.min(ROWS-1,Math.ceil((cz+reach+HEIGHT/2)/CELL));

    for (let j=j0;j<=j1;j++) for (let i=i0;i<=i1;i++){
      const k = idx(i,j);
      if (!cellIsGround[k]) continue;
      const [x,z]=cellCentre(i,j);
      const dx=x-cx, dz=z-cz;
      const along = (dx*SHX + dz*SHZ) / semiMajor;
      const across = (dx*(-SHZ) + dz*SHX) / r;
      const d = Math.sqrt(along*along + across*across);
      if (d >= 1) continue;
      const cover = d < 0.86 ? 1 : (1 - d) / 0.14;
      cb(k, cover);
    }
  }

  function applyTree(t, growth){
    const sp = t.sp;
    const crown = sp.crown * growth;
    const trunkH = sp.trunk * Math.max(growth, 0.3);
    const crownCentreHeight = trunkH + (sp.height * growth - trunkH) * 0.55;
    const r = Math.max(0.3, crown / 2);
    forEachEllipseCell(t.x, t.z, crownCentreHeight, r, (k, cover) => {
      openSky[k] *= (1 - cover * (1 - sp.tau));
    });
  }

  function recomputeAll(){
    openSky.fill(1);
    const growth = growthFraction(Number(yearsInput.value));
    for (const t of trees) applyTree(t, growth);
    for (let k=0;k<NC;k++) shadeFrac[k] = 1 - openSky[k];
  }

  // Existing-tree removal: a Set of `existingTrees` indices per scenario the
  // user has removed (independent per scenario -- the two scenarios' tree
  // lists don't correspond index-for-index). Uses the site's real canopy
  // transmissivity (the value the actual SOLWEIG run used for every existing
  // tree), not a species-specific one -- existing trees have no recorded
  // species, just height/crown from CDSM peak detection.
  const removedByScenario = {};
  for (const name of scenarioNames) removedByScenario[name] = new Set();
  const EXISTING_TREE_TAU = manifest.site.existingTreeTransmissivity;

  // Only cells inside a removed tree's own shadow-casting footprint are ever
  // touched -- everywhere else keeps the exact real measured tmrtBaseline, no
  // reconstruction (same "not a reconstruction" guarantee tmrtAt already made
  // for unplanted cells). For a touched cell, divide that tree's implied
  // coverage back out of the cell's implied openSkyBase (the exact inverse of
  // the "openSky *=" used to add a new tree's coverage in), clamp to a real
  // openSky fraction (removing shade can cool a cell at most to its fully-
  // sunlit endpoint, never past it), then re-derive Tmrt via the same flux
  // blend. Overlapping removed trees on the same cell compose multiplicatively,
  // matching CLAUDE.md's "combining shadows" rule.
  function recomputeBaselineEff(){
    const S = SCEN;
    const out = Float32Array.from(S.tmrtBaseline);
    const touched = new Map(); // cell index -> combined divisor factor
    removedByScenario[scenarioName].forEach(i => {
      const t = S.existingTrees[i];
      forEachEllipseCell(t.cx, t.cz, t.height * 0.6, t.crown, (k, cover) => {
        const factor = 1 - cover * (1 - EXISTING_TREE_TAU);
        touched.set(k, (touched.get(k) || 1) * factor);
      });
    });
    touched.forEach((factor, k) => {
      if (factor <= 1e-3) return;
      const openSkyAdj = Math.min(1, S.openSkyBase[k] / factor);
      const f = Math.max(0, 1 - openSkyAdj);
      const a = S.tmrtSun[k] + 273.15, b = S.tmrtShade[k] + 273.15;
      const q = (1 - f) * a*a*a*a + f * b*b*b*b;
      out[k] = Math.pow(q, 0.25) - 273.15;
    });
    S.tmrtBaselineEff = out;
  }

  // Blends from the effective baseline (real measured Tmrt, adjusted for any
  // removed existing trees) toward the shaded endpoint by the marginal
  // coverage NEW trees add. At f=0 with nothing removed this returns the real
  // measured Tmrt exactly, not a reconstruction.
  function tmrtAt(k){
    const f = shadeFrac[k];
    const base = SCEN.tmrtBaselineEff[k];
    if (f <= 0) return base;
    if (f >= 1) return SCEN.tmrtShade[k];
    const a = base + 273.15, b = SCEN.tmrtShade[k] + 273.15;
    const q = (1 - f) * a*a*a*a + f * b*b*b*b;
    return Math.pow(q, 0.25) - 273.15;
  }

  /* ==========================================================================
     SCENE
     ======================================================================= */
  const stage = document.getElementById('stage');
  const renderer = new THREE.WebGLRenderer({ antialias:true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  stage.appendChild(renderer.domElement);

  const diag = Math.hypot(WIDTH, HEIGHT);
  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0e1116);
  // fog range must scale with the actual camera-to-ground distance (orbitDist,
  // set below from the domain diagonal) -- a fixed range tuned for a smaller
  // domain washes the whole ground out to the background color.
  scene.fog = new THREE.Fog(0x0e1116, diag * 1.1, diag * 3.2);

  const planCam = new THREE.PerspectiveCamera(38, 1, 1, 3000);
  const walkCam = new THREE.PerspectiveCamera(72, 1, 0.1, 1200);
  let camera = planCam, mode = 'plan';

  scene.add(new THREE.AmbientLight(0xb8c6d4, 0.72));
  const sunLight = new THREE.DirectionalLight(0xfff0d8, 0.85);
  sunLight.position.set(Math.cos(altRad)*Math.sin(aziRad)*300, Math.sin(altRad)*300, -Math.cos(altRad)*Math.cos(aziRad)*300);
  scene.add(sunLight);

  // Real aerial imagery, sitting just below the thermal ground plane -- the
  // "Aerial imagery" slider fades the heat map's opacity to reveal it, so you
  // can see the actual park underneath the data (Esri World Imagery, fetched
  // once in scripts/fetch_basemap.py, pixel-aligned to the site in EPSG:6343).
  const basemapTex = new THREE.TextureLoader().load(DATA_DIR + 'basemap.jpg');
  basemapTex.colorSpace = THREE.SRGBColorSpace;
  const basemapMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(WIDTH, HEIGHT),
    new THREE.MeshBasicMaterial({ map: basemapTex, fog: false })
  );
  basemapMesh.rotation.x = -Math.PI / 2;
  basemapMesh.position.y = -0.05;
  scene.add(basemapMesh);

  // Thermal ground
  const texData = new Uint8Array(NC * 4);
  const groundTex = new THREE.DataTexture(texData, COLS, ROWS, THREE.RGBAFormat);
  groundTex.magFilter = THREE.LinearFilter;
  groundTex.minFilter = THREE.LinearFilter;
  const groundMesh = new THREE.Mesh(
    new THREE.PlaneGeometry(WIDTH, HEIGHT),
    new THREE.MeshBasicMaterial({ map: groundTex, fog: false, transparent: true })
  );
  groundMesh.rotation.x = -Math.PI / 2;
  scene.add(groundMesh);

  // Buildings — real footprint polygons (ring: [[x,z],...] in local scene coords) extruded
  // to their real ROOF_HEIGHT, from Building_Vector.fgb. Each gets its own bbox precomputed
  // for cheap walk-mode collision.
  const bMat = new THREE.MeshLambertMaterial({ color:0x6b7480 });
  const bGroup = new THREE.Group();
  const buildingBoxes = []; // {x0,x1,z0,z1}
  for (const b of BUILDINGS) {
    const shape = new THREE.Shape();
    b.ring.forEach(([x, z], i) => {
      if (i === 0) shape.moveTo(x, -z); else shape.lineTo(x, -z);
    });
    const geo = new THREE.ExtrudeGeometry(shape, { depth: b.h, bevelEnabled: false });
    geo.rotateX(-Math.PI / 2);
    const mesh = new THREE.Mesh(geo, bMat);
    bGroup.add(mesh);
    let x0=Infinity,x1=-Infinity,z0=Infinity,z1=-Infinity;
    for (const [x,z] of b.ring) { if(x<x0)x0=x; if(x>x1)x1=x; if(z<z0)z0=z; if(z>z1)z1=z; }
    buildingBoxes.push({x0,x1,z0,z1});
  }
  scene.add(bGroup);

  // Existing trees (decorative context; their effect is already baked into tmrtSun/tmrtShade).
  // Pool sized to the larger of the two scenarios' tree counts; rebuilt on scenario switch.
  const exTrunk = new THREE.InstancedMesh(
    new THREE.CylinderGeometry(0.15, 0.22, 1, 6),
    new THREE.MeshLambertMaterial({ color:0x3a3226 }), Math.max(1, MAX_EXISTING_TREES));
  const exCrown = new THREE.InstancedMesh(
    new THREE.SphereGeometry(1, 10, 8),
    new THREE.MeshLambertMaterial({ color:0x33502e }), Math.max(1, MAX_EXISTING_TREES));
  scene.add(exTrunk, exCrown);

  function rebuildExistingTrees(){
    const list = SCEN.existingTrees;
    const removed = removedByScenario[scenarioName];
    const m = new THREE.Matrix4();
    let n = 0;
    list.forEach((t, i) => {
      if (removed.has(i)) return;
      const trunkH = Math.max(1, t.height * 0.3);
      m.makeScale(1, trunkH, 1).setPosition(t.cx, trunkH/2, t.cz);
      exTrunk.setMatrixAt(n, m);
      m.makeScale(t.crown, t.height * 0.6, t.crown).setPosition(t.cx, t.height * 0.6, t.cz);
      exCrown.setMatrixAt(n, m);
      n++;
    });
    exTrunk.count = exCrown.count = n;
    exTrunk.instanceMatrix.needsUpdate = true;
    exCrown.instanceMatrix.needsUpdate = true;
  }

  // Planted trees
  const MAX_TREES = 500;
  const trunkMesh = new THREE.InstancedMesh(
    new THREE.CylinderGeometry(0.18, 0.26, 1, 6),
    new THREE.MeshLambertMaterial({ color:0x4a3f33 }), MAX_TREES);
  trunkMesh.count = 0;
  scene.add(trunkMesh);

  // One InstancedMesh per species for tree crowns, each with its own flat
  // material.color (sp.leaf), instead of a single shared mesh relying on
  // per-instance vertex/instanceColor. Per-instance color on non-Basic
  // materials needs three.js r131+ and can silently render solid black on
  // older builds (or if the color buffer isn't wired up right) even though
  // per-instance matrices/sizing still work fine -- this sidesteps that
  // failure mode entirely, at the cost of one InstancedMesh per species.
  const crownGeo = new THREE.SphereGeometry(1, 12, 9);
  const crownMeshBySpecies = {};
  SPECIES.forEach(sp => {
    const mesh = new THREE.InstancedMesh(crownGeo, new THREE.MeshLambertMaterial({ color: sp.leaf }), MAX_TREES);
    mesh.count = 0;
    crownMeshBySpecies[sp.id] = mesh;
    scene.add(mesh);
  });

  function rebuildTreeMeshes(){
    const growth = growthFraction(Number(yearsInput.value));
    const m = new THREE.Matrix4();

    // per-species instance counters -- a tree's index within its species'
    // InstancedMesh is NOT the same as its index in `trees` (that array mixes
    // species), so each species pool tracks its own running count.
    const speciesIndex = {};
    SPECIES.forEach(sp => { speciesIndex[sp.id] = 0; });

    trees.forEach((t, i) => {
      const sp = t.sp;
      const trunkH = Math.max(0.3, sp.trunk * Math.max(growth, 0.3));
      const crownR = Math.max(0.3, sp.crown * growth / 2);
      const crownY = trunkH + (sp.height * growth - trunkH) * 0.55;

      m.makeScale(1, trunkH, 1).setPosition(t.x, trunkH/2, t.z);
      trunkMesh.setMatrixAt(i, m);

      const crownMesh = crownMeshBySpecies[sp.id];
      const ci = speciesIndex[sp.id]++;
      m.makeScale(crownR, Math.max(0.3, (sp.height - sp.trunk) * growth * 0.62), crownR).setPosition(t.x, crownY, t.z);
      crownMesh.setMatrixAt(ci, m);
    });

    trunkMesh.count = trees.length;
    trunkMesh.instanceMatrix.needsUpdate = true;

    SPECIES.forEach(sp => {
      const mesh = crownMeshBySpecies[sp.id];
      mesh.count = speciesIndex[sp.id];
      mesh.instanceMatrix.needsUpdate = true;
    });
  }

  /* ==========================================================================
     PAINT + STATS
     ======================================================================= */
  let showUncertainty = false;
  let baseMean = 0, baseExtreme = 0, baseSevere = 0, baseShaded = 0, groundCount = 0;

  function paint(){
    let sum = 0, extreme = 0, severe = 0, shaded = 0, count = 0;
    for (let j = 0; j < ROWS; j++) for (let i = 0; i < COLS; i++) {
      const k = idx(i,j);
      const p = ((ROWS - 1 - j) * COLS + i) * 4;
      if (!cellIsGround[k]) { texData[p]=38; texData[p+1]=42; texData[p+2]=50; texData[p+3]=255; continue; }
      if (showUncertainty) {
        const c = uncertaintyColor(SCEN.uncertainty[k]);
        texData[p]=c[0]; texData[p+1]=c[1]; texData[p+2]=c[2]; texData[p+3]=255;
      } else {
        const u = SCEN.utciFromTmrt(tmrtAt(k));
        const c = bandFor(u).rgb;
        const t = 0.82 + 0.18 * Math.min(1, Math.max(0, (u - 24) / 28));
        texData[p]=c[0]*t; texData[p+1]=c[1]*t; texData[p+2]=c[2]*t; texData[p+3]=255;
        sum += u; count++;
        if (u >= 46) extreme++;
        if (u >= 38) severe++;
      }
      if (shadeFrac[k] > 0.5) shaded++;
    }
    groundTex.needsUpdate = true;
    groundCount = count || 1;
    return { mean: sum/groundCount, extreme: extreme/groundCount, severe: severe/groundCount, shaded: shaded/groundCount };
  }

  function refreshStats(s){
    const f = (v) => (v*100).toFixed(1) + '%';
    // A single tree's effect on a site-wide average over ~50k cells is real
    // but tiny (site is ~200,000 m², one mature tree shades ~100-300 m²) --
    // 2 decimals rounds that to "-0.00" and looks like nothing happened even
    // though it did (see s-shade / the hover readout for the actual local
    // effect). 3 decimals on the delta keeps it visible; plant more trees or
    // check "New shade" / hover the ground for a clearer signal on one tree.
    // Deltas are relative to the real measured baseline, so both planting and
    // removing existing trees move them -- keyed off either count, not just
    // trees.length.
    const changed = trees.length || removedByScenario[scenarioName].size;
    document.getElementById('s-trees').textContent = trees.length;

    const counts = {};
    SPECIES.forEach(sp => counts[sp.id] = 0);
    for (const t of trees) {
      counts[t.sp.id]++;
    }
    SPECIES.forEach(sp => {
      const el = document.getElementById(`s-trees-${sp.id}`);
      if (el) el.textContent = counts[sp.id];
    });

    document.getElementById('s-mean').innerHTML = s.mean.toFixed(1) + ' °C' +
      (changed ? `<span class="delta">${(s.mean-baseMean).toFixed(3)}</span>` : '');
    document.getElementById('s-extreme').innerHTML = f(s.extreme) +
      (changed ? `<span class="delta">${((s.extreme-baseExtreme)*100).toFixed(2)}</span>` : '');
    document.getElementById('s-severe').innerHTML = f(s.severe) +
      (changed ? `<span class="delta">${((s.severe-baseSevere)*100).toFixed(2)}</span>` : '');
    const area = (s.shaded - baseShaded) * groundCount * CELL * CELL;
    document.getElementById('s-shade').textContent = trees.length ? Math.round(area).toLocaleString() + ' m²' : '—';
    document.getElementById('undo').disabled = !trees.length;
    document.getElementById('clear').disabled = !trees.length;
  }

  function refreshAll(){
    recomputeAll();
    rebuildTreeMeshes();
    refreshStats(paint());
  }

  // The scenario's own baseline (mean/extreme/severe at zero NEW trees) --
  // used as the reference point for the delta numbers shown in the stats
  // panel. Computed fresh from SCEN.tmrtBaseline directly, independent of
  // whatever new trees are currently planted (those persist across a
  // scenario switch and are re-evaluated against the new baseline).
  function computeBaseline(){
    let sum=0, extreme=0, severe=0, count=0;
    for (let k=0;k<NC;k++){
      if (!cellIsGround[k]) continue;
      const u = SCEN.utciFromTmrt(SCEN.tmrtBaseline[k]);
      sum+=u; count++;
      if (u>=46) extreme++;
      if (u>=38) severe++;
    }
    return { mean: sum/count, extreme: extreme/count, severe: severe/count };
  }

  function setScenario(name){
    scenarioName = name;
    SCEN = scenarioData[name];
    rebuildExistingTrees();
    recomputeBaselineEff();
    updateRemovedCount();
    const b = computeBaseline();
    baseMean = b.mean; baseExtreme = b.extreme; baseSevere = b.severe; baseShaded = 0;
    refreshAll();
    for (const n of scenarioNames) document.getElementById('scen-'+n).setAttribute('aria-pressed', n === name);
    document.getElementById('scenario-note').textContent =
      `${SCEN.label} · baseline mean UTCI ${SCEN.meanUtciBaseline.toFixed(1)} °C`;
  }

  /* ==========================================================================
     INTERACTION
     ======================================================================= */
  const ray = new THREE.Raycaster(), ndc = new THREE.Vector2();
  let currentSpecies = SPECIES[0];
  let tool = 'plant'; // 'plant' | 'remove'

  function plantAt(x, z){
    if (Math.abs(x) > WIDTH/2 - 2 || Math.abs(z) > HEIGHT/2 - 2) return;
    if (trees.length >= MAX_TREES) return;
    const i = Math.floor((x+WIDTH/2)/CELL), j = Math.floor((z+HEIGHT/2)/CELL);
    if (i<0||i>=COLS||j<0||j>=ROWS) return;
    const k = idx(i, j);
    if (!cellIsGround[k]) return;
    trees.push({ x, z, sp: currentSpecies });
    refreshAll();
  }

  function undo(){ if (!trees.length) return; trees.pop(); refreshAll(); }
  function clearAll(){ trees.length = 0; refreshAll(); }

  // Nearest existing tree to a click/hover point, within its own crown radius
  // (min 2 m, so even a small crown stays clickable). Returns -1 if none.
  function nearestExistingTree(x, z){
    const list = SCEN.existingTrees;
    let best = -1, bestD = Infinity;
    for (let i = 0; i < list.length; i++) {
      const t = list[i];
      const d = Math.hypot(t.cx - x, t.cz - z);
      const reach = Math.max(2, t.crown);
      if (d <= reach && d < bestD) { bestD = d; best = i; }
    }
    return best;
  }

  function updateRemovedCount(){
    const n = removedByScenario[scenarioName].size;
    document.getElementById('s-removed').textContent = n;
    document.getElementById('restore-removed').disabled = !n;
  }

  function toggleRemoveAt(x, z){
    const i = nearestExistingTree(x, z);
    if (i < 0) return;
    const removed = removedByScenario[scenarioName];
    if (removed.has(i)) removed.delete(i); else removed.add(i);
    rebuildExistingTrees();
    recomputeBaselineEff();
    refreshStats(paint());
    updateRemovedCount();
  }

  // Hover highlight fo the remove tool: a flat ring on the ground under
  // whichever existing tree the cursor is currently over (nearestExistingTree
  // result), so it's clear which tree a click will act on.
  // depthTest:false + high renderOrder keeps it visible through canopy/ground
  // overlap rather than fighting z-buffer precision at this scale
  const hoverRing = new THREE.Mesh(
    new THREE.RingGeometry(0.85, 1, 32),
    new THREE.MeshBasicMaterial({ color:0xffcc33, side:THREE.DoubleSide, transparent:true,
      opacity:0.9, depthTest:false, fog:false})
  );
  hoverRing.rotation.x = -Math.PI / 2;
  hoverRing.renderOrder = 999;
  hoverRing.visible = false;
  scene.add(hoverRing);

  renderer.domElement.addEventListener('click', (e) => {
    if (mode !== 'plan') return;
    const r = renderer.domElement.getBoundingClientRect();
    ndc.x = ((e.clientX - r.left)/r.width)*2 - 1;
    ndc.y = -((e.clientY - r.top)/r.height)*2 + 1;
    ray.setFromCamera(ndc, camera);
    const hit = ray.intersectObject(groundMesh)[0];
    if (!hit) return;
    if (tool === 'remove') {
      const ti = nearestExistingTree(hit.point.x, hit.point.z);
      toggleRemoveAt(hit.point.x, hit.point.z);
      if (ti >= 0) {
        const t = SCEN.existingTrees[ti];
        const isRemoved = removedByScenario[scenarioName].has(ti);
        hoverRing.material.color.set(isRemoved ? 0xff5544 : 0xffcc33);
        pointReadout.innerHTML = isRemoved
          ? `<b>Removed</b><br>click to restore`
          : `<b>${t.height.toFixed(1)} m tree</b><br>click to remove`;
      }
    }
    else plantAt(hit.point.x, hit.point.z);
  });

  // Hover readout: a single tree's effect on the site-wide average is real
  // but tiny (see refreshStats comment) -- this shows the exact local UTCI
  // at the cursor so a single planting's effect is directly visible, not
  // diluted into a site-wide mean.

  const pointReadout = document.getElementById('point-readout');
  renderer.domElement.addEventListener('mousemove', (e) => {
    if (mode !== 'plan' || dragging) { pointReadout.style.display = 'none'; hoverRing.visible 
      = false; return; }
    const r = renderer.domElement.getBoundingClientRect();
    ndc.x = ((e.clientX - r.left)/r.width)*2 - 1;
    ndc.y = -((e.clientY - r.top)/r.height)*2 + 1;
    ray.setFromCamera(ndc, camera);
    const hit = ray.intersectObject(groundMesh)[0];
    if (!hit) { pointReadout.style.display = 'none'; hoverRing.visible = false; return; }
    if (tool === 'remove') {
      const ti = nearestExistingTree(hit.point.x, hit.point.z);
      if (ti < 0) { pointReadout.style.display = 'none'; hoverRing.visible = false; return; }
      const t = SCEN.existingTrees[ti];
      const isRemoved = removedByScenario[scenarioName].has(ti);
      hoverRing.visible = true;
      hoverRing.position.set(t.cx, 0.08, t.cz);
      const reach = Math.max(2, t.crown); // Matches nearestExistingTree's own reach radius
      hoverRing.scale.set(reach, reach, 1);
      hoverRing.material.color.set(isRemoved ? 0xff5544 : 0xffcc33);
      pointReadout.style.left = e.clientX + 'px';
      pointReadout.style.top = e.clientY + 'px';
      pointReadout.style.display = 'block';
      pointReadout.innerHTML = isRemoved
        ? `<b>Removed</b><br>click to restore`
        : `<b>${t.height.toFixed(1)} m tree</b><br>click to remove`;
      return;
    }
    hoverRing.visible = false;
    const i = Math.floor((hit.point.x+WIDTH/2)/CELL), j = Math.floor((hit.point.z+HEIGHT/2)/CELL);
    if (i<0||i>=COLS||j<0||j>=ROWS) { pointReadout.style.display = 'none'; return; }
    const k = idx(i,j);
    if (!cellIsGround[k]) { pointReadout.style.display = 'none'; return; }
    const u = SCEN.utciFromTmrt(tmrtAt(k)), band = bandFor(u);
    pointReadout.style.left = e.clientX + 'px';
    pointReadout.style.top = e.clientY + 'px';
    pointReadout.style.display = 'block';
    pointReadout.innerHTML = `<b>${u.toFixed(1)} °C</b><br>${band.label}`;
  });
  renderer.domElement.addEventListener('mouseleave', () => { pointReadout.style.display = 'none';
    hoverRing.visible = false; });

  // Plan-view orbit
  let dragging = false, lastX = 0, lastY = 0, orbitYaw = 0.72, orbitPitch = 0.82, orbitDist = diag * 0.85;
  const orbitTarget = new THREE.Vector3(0, 0, 0); // WASD-panned pivot points
  renderer.domElement.addEventListener('pointerdown', e => {
    if (mode !== 'plan') return;
    dragging = true; lastX = e.clientX; lastY = e.clientY;
    renderer.domElement.setPointerCapture(e.pointerId);
  });
  renderer.domElement.addEventListener('pointerup', e => {
    dragging = false;
    try { renderer.domElement.releasePointerCapture(e.pointerId); } catch(_) {}
  });
  renderer.domElement.addEventListener('pointermove', e => {
    if (!dragging || mode !== 'plan') return;
    orbitYaw   -= (e.clientX - lastX) * 0.005;
    orbitPitch = Math.min(1.45, Math.max(0.22, orbitPitch - (e.clientY - lastY) * 0.004));
    lastX = e.clientX; lastY = e.clientY;
    placeOrbit();
  });
  renderer.domElement.addEventListener('wheel', e => {
    if (mode !== 'plan') return;
    e.preventDefault();
    orbitDist = Math.min(diag*1.8, Math.max(diag*0.15, orbitDist * (1 + Math.sign(e.deltaY)*0.09)));
    placeOrbit();
  }, { passive:false });

  

  function placeOrbit(){
    planCam.position.set(
      orbitTarget.x + Math.sin(orbitYaw)*Math.cos(orbitPitch)*orbitDist,
      orbitTarget.y + Math.sin(orbitPitch)*orbitDist,
      orbitTarget.z + Math.cos(orbitYaw)*Math.cos(orbitPitch)*orbitDist);
    planCam.lookAt(orbitTarget);
  }

  // WASD panning in plan mode: moves orbitTarget across the ground plane,
  // relative to the camera's current yaw (W = "into the screen" at whatever
  // angle you're currently looking from, not fixed to world Z). Shares the
  // same `keys` state that drives walk-mode movement below -- safe since
  // stepPlanPan only runs while mode === 'plan' and stepWalk only while
  // mode === 'walk', so the two never read the held keys at the same time.
  const PAN_SPEED = 1; // fraction of orbitDist covered per second at full speed
  const YAW_SPEED = 1.4; // radians per second at full speed
  function stepPlanPan(dt){
   
   if (keys.KeyQ) orbitYaw += YAW_SPEED * dt;
   if (keys.KeyE) orbitYaw -= YAW_SPEED * dt;

   const forward = new THREE.Vector3();
   const right = new THREE.Vector3();

   // Get forward direction from camera
   planCam.getWorldDirection(forward);
   forward.y = 0; // Prevents up/down movement
   forward.normalize();

   // Get right direction from forward vector
   right.crossVectors(forward, planCam.up).normalize();

   const moveVec = new THREE.Vector3();

   // Move forward/backward
   if (keys.KeyW || keys.ArrowUp) {
    planCam.position.add(forward.clone().multiplyScalar(PAN_SPEED));
    moveVec.add(forward);
   }
   if (keys.KeyS || keys.ArrowDown) {
    planCam.position.add(forward.clone().multiplyScalar(-PAN_SPEED));
    moveVec.sub(forward);
   }
   // Move Left/Right
   if (keys.KeyA || keys.ArrowLeft) {
    planCam.position.add(right.clone().multiplyScalar(-PAN_SPEED));
    moveVec.sub(right);
   }
   if (keys.KeyD || keys.ArrowRight) {
    planCam.position.add(right.clone().multiplyScalar(PAN_SPEED));
    moveVec.add(right);
   }

   if (moveVec.lengthSq() > 0) {
    moveVec.normalize();
   }

   const speed = PAN_SPEED * orbitDist * dt;
   moveVec.multiplyScalar(speed);

   orbitTarget.add(moveVec);
   placeOrbit();
  }

  // Walk mode
  const keys = Object.create(null);
  let walkYaw = 0, walkPitch = 0;
  const walkPos = new THREE.Vector3(0, 1.65, 0);

  addEventListener('keydown', e => { keys[e.code] = true; });
  addEventListener('keyup',   e => { keys[e.code] = false; });

  document.addEventListener('pointerlockchange', () => {
    if (mode === 'walk' && !document.pointerLockElement) setMode('plan');
  });
  document.addEventListener('mousemove', e => {
    if (mode !== 'walk' || !document.pointerLockElement) return;
    walkYaw   -= e.movementX * 0.0022;
    walkPitch = Math.max(-1.3, Math.min(1.3, walkPitch - e.movementY * 0.0022));
  });

  function setMode(next){
    mode = next;
    const plan = next === 'plan';
    if (!plan) hoverRing.visible = false;
    camera = plan ? planCam : walkCam;
    document.getElementById('m-plan').setAttribute('aria-pressed', plan);
    document.getElementById('m-walk').setAttribute('aria-pressed', !plan);
    document.getElementById('hint').style.display = plan ? '' : 'none';
    document.getElementById('hud').style.display = plan ? 'none' : 'block';
    document.getElementById('walkbar').style.display = plan ? 'none' : 'flex';
    if (plan && document.pointerLockElement) document.exitPointerLock();
    if (!plan) {
      const p = renderer.domElement.requestPointerLock();
      if (p && p.catch) p.catch(() => {}); // some embedding contexts (iframes) deny pointer lock
    }
  }
  document.getElementById('m-plan').onclick = () => setMode('plan');
  document.getElementById('m-walk').onclick = () => setMode('walk');

  function blocked(x, z){
    for (const b of buildingBoxes)
      if (x > b.x0-0.6 && x < b.x1+0.6 && z > b.z0-0.6 && z < b.z1+0.6) return true;
    return false;
  }

  function stepWalk(dt){
    // Start with updating camera to ensure getWorldDirection is up to date
    walkCam.rotation.set(walkPitch, walkYaw, 0, 'YXZ');

    const speed = (keys.ShiftLeft ? 11 : 4.6) * dt;

    const forward = new THREE.Vector3();
    const right = new THREE.Vector3();

    // Get forward direction from camera
    walkCam.getWorldDirection(forward);
    forward.y = 0;
    forward.normalize();
    // Get right direction from forward vector
    right.crossVectors(forward, walkCam.up).normalize();

    const moveVec = new THREE.Vector3();

    // Movement
    if (keys.KeyW || keys.ArrowUp) moveVec.add(forward);
    if (keys.KeyS || keys.ArrowDown) moveVec.sub(forward);
    if (keys.KeyA || keys.ArrowLeft) moveVec.sub(right);
    if (keys.KeyD || keys.ArrowRight) moveVec.add(right);

    // Apply movement vector
    if (moveVec.lengthSq() > 0) {
      moveVec.normalize();
      moveVec.multiplyScalar(speed);

      const nx = walkPos.x + moveVec.x;
      const nz = walkPos.z + moveVec.z;

      // Handle collision
      if (!blocked(nx, walkPos.z)) walkPos.x = nx;
      if (!blocked(walkPos.x, nz)) walkPos.z = nz;

      walkPos.x = Math.max(-WIDTH/2+1, Math.min(WIDTH/2-1, walkPos.x));
      walkPos.z = Math.max(-HEIGHT/2+1, Math.min(HEIGHT/2-1, walkPos.z));
    }

    // Update camera to match updated walkPos
    walkCam.position.copy(walkPos);

    // UTCI readout
    const i = Math.floor((walkPos.x+WIDTH/2)/CELL), j = Math.floor((walkPos.z+HEIGHT/2)/CELL);
    const k = idx(Math.max(0,Math.min(COLS-1,i)), Math.max(0,Math.min(ROWS-1,j)));
    const u = SCEN.utciFromTmrt(tmrtAt(k)), band = bandFor(u);
    document.getElementById('w-utci').textContent = u.toFixed(1) + ' °C';
    const el = document.getElementById('w-cat');
    el.textContent = band.label;
    el.style.color = `rgb(${band.rgb.join(',')})`;
  }

  /* ==========================================================================
     UI WIRING
     ======================================================================= */
  const sel = document.getElementById('species');
  SPECIES.forEach(s => sel.add(new Option(s.name, s.id)));

  const speciesCountsContainer = document.getElementById('species-counts');
  if (speciesCountsContainer) {
    SPECIES.forEach(sp => {
      const wrapper = document.createElement('div');
      wrapper.className = 'stat'; // Use the existing CSS layout!
      wrapper.style.paddingLeft = '12px'; // Indent slightly to look like a sub-list
      wrapper.style.borderBottom = 'none'; // Remove borders between individual species
      
      // Format to perfectly match the other labels and numbers
      wrapper.innerHTML = `<span>${sp.name}</span><b class="mono" id="s-trees-${sp.id}">0</b>`;
      speciesCountsContainer.appendChild(wrapper);
    });
  }
  function showSpecs(){
    const s = currentSpecies;
    document.getElementById('specs').innerHTML =
      `<b>Mature crown</b><span>${s.crown.toFixed(1)} m</span>` +
      `<b>Mature height</b><span>${s.height.toFixed(1)} m</span>` +
      `<b>Trunk</b><span>${s.trunk.toFixed(1)} m</span>` +
      `<b>Transmissivity</b><span>${(s.tau*100).toFixed(0)}%</span>`;
  }
  sel.onchange = () => { currentSpecies = SPECIES.find(s => s.id === sel.value); showSpecs(); };
  showSpecs();

  const yearsInput = document.getElementById('years');
  yearsInput.oninput = () => {
    document.getElementById('years-val').textContent = yearsInput.value;
    refreshAll();
  };

  document.getElementById('undo').onclick = undo;
  document.getElementById('clear').onclick = clearAll;
  addEventListener('keydown', e => {
    if (mode === 'plan' && tool === 'plant' && (e.metaKey || e.ctrlKey) && e.key === 'z') { e.preventDefault(); undo(); }
  });

  const toolBtns = { plant: document.getElementById('t-plant'), remove: document.getElementById('t-remove') };
  function setTool(next){
    tool = next;
    if (tool !== 'remove') hoverRing.visible = false;
    toolBtns.plant.setAttribute('aria-pressed', tool === 'plant');
    toolBtns.remove.setAttribute('aria-pressed', tool === 'remove');
    document.getElementById('plant-controls').style.display = tool === 'plant' ? '' : 'none';
    document.getElementById('remove-note').style.display = tool === 'remove' ? '' : 'none';
    document.getElementById('remove-controls').style.display = tool === 'remove' ? '' : 'none';
    document.getElementById('hint').textContent = tool === 'plant'
      ? 'Click the ground to plant · hover to inspect UTCI'
      : 'Click an existing tree to remove it · click again to restore';
  }
  toolBtns.plant.onclick = () => setTool('plant');
  toolBtns.remove.onclick = () => setTool('remove');
  document.getElementById('restore-removed').onclick = () => {
    removedByScenario[scenarioName].clear();
    rebuildExistingTrees();
    recomputeBaselineEff();
    refreshStats(paint());
    updateRemovedCount();
  };

  const uncBtn = document.getElementById('toggle-uncertainty');
  uncBtn.onclick = () => {
    showUncertainty = !showUncertainty;
    uncBtn.setAttribute('aria-pressed', showUncertainty);
    uncBtn.textContent = showUncertainty ? 'Show UTCI' : 'Show uncertainty';
    updateLegend();
    refreshStats(paint());
  };

  const basemapInput = document.getElementById('basemap');
  basemapInput.oninput = () => {
    const pct = Number(basemapInput.value);
    document.getElementById('basemap-val').textContent = pct + '%';
    groundMesh.material.opacity = 1 - pct / 100;
  };

  function updateLegend(){
    if (showUncertainty) {
      document.getElementById('legend-title').textContent = 'Endpoint imputation uncertainty';
      document.getElementById('ramp').innerHTML =
        UNCERTAINTY_RAMP.map(([,c]) => `<i style="background:rgb(${c.join(',')})"></i>`).join('');
      document.getElementById('ramp-ticks').innerHTML =
        UNCERTAINTY_RAMP.map(([t]) => `<span>${t}K</span>`).join('');
      document.getElementById('legend-note').textContent =
        'Sun/shade endpoints are approximated from this scene’s existing trees (path b), not a second SOLWEIG run. Darker = less trustworthy, typically near buildings.';
    } else {
      document.getElementById('legend-title').textContent = 'UTCI heat stress';
      document.getElementById('ramp').innerHTML =
        BANDS.map(b => `<i style="background:rgb(${b.rgb.join(',')})" title="${b.label}"></i>`).join('');
      document.getElementById('ramp-ticks').innerHTML = '<span>26</span><span>32</span><span>38</span><span>46</span><span>52</span>';
      document.getElementById('legend-note').textContent =
        `${manifest.site.hourLabel} · Ta ${manifest.site.airTempC} °C · RH ${manifest.site.rhPct}% · wind ${manifest.site.windMs} m/s · sun ${manifest.site.sunAltitudeDeg.toFixed(0)}° alt, ${manifest.site.sunAzimuthDeg.toFixed(0)}° az`;
    }
  }

  const scenSeg = document.getElementById('scenario-seg');
  scenarioNames.forEach(name => {
    const btn = document.createElement('button');
    btn.id = 'scen-' + name;
    btn.textContent = scenarioData[name].label;
    btn.setAttribute('aria-pressed', name === scenarioName);
    btn.onclick = () => setScenario(name);
    scenSeg.appendChild(btn);
  });

  document.getElementById('hour-label').textContent = manifest.site.hourLabel;
  document.getElementById('limits-list').innerHTML = manifest.knownLimits.map(l => `<li>${l}</li>`).join('');
  updateLegend();

  function resize(){
    const w = innerWidth || 1280, h = innerHeight || 800;
    renderer.setSize(w, h);
    planCam.aspect = walkCam.aspect = w/h;
    planCam.updateProjectionMatrix(); walkCam.updateProjectionMatrix();
  }
  addEventListener('resize', resize);
  resize();
  placeOrbit();

  setScenario(scenarioName);

  document.getElementById('loading').style.display = 'none';
  for (const id of ['scenario','tools','readout','legend','limits']) document.getElementById(id).style.display = '';

  let prev = performance.now();
  (function loop(now){
    requestAnimationFrame(loop);
    const dt = Math.min(0.05, (now - prev)/1000); prev = now;
    if (mode === 'walk') stepWalk(dt);
    if (mode === 'plan') stepPlanPan(dt);
    renderer.render(scene, camera);
  })(performance.now());
}

main().catch(err => {
  document.getElementById('loading').textContent = 'Failed to load: ' + err.message;
  console.error(err);
});