import { useEffect, useRef } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DObject, CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import { andon, buildMachine, car, forklift, gate, operator, packing, truck, walker } from "./floorModels.js";

// 3D digital twin of ONE plant (props.plant), opened from the map. The plant is a walled compound on a public road: fence, gate and guard hut, a yard where lorries
// reverse into the dock doors, visitor parking, and the plant building. Zoom in and the roof and walls fade away to show the whole
// production unit: receiving and stores, the line bays (Hosur 3, Bengaluru 5), the WIP buffer, packing, pallet staging, finished goods
// and shipping. Hosur is a low hall with a pitched roof, selective racks and a bulk floor store; Bengaluru a taller hall with a sawtooth
// roof and high-bay racks.
//
// Driven by the recorded hour (props.snap): line states, conveyor pace, andon, operators, rejects, rack fill, pallet staging, and
// whether lorries run (stock in transit / orders due). The data records two lines per plant (SMT + final assembly); the other bays
// run on generated records. People walking around and lorry timing are illustrative.
// Colours are read from the theme tokens at runtime, so light and dark themes both apply.

const SIZE = { SPI: [1.6, 1.2, 1.4], "Pick and Place": [2.6, 1.5, 1.8], "Reflow Oven": [3.4, 1.2, 1.5], AOI: [1.6, 1.4, 1.5], "ICT Tester": [1.6, 1.5, 1.4],
  "Assembly Cell": [2.4, 2.0, 2.2], "RF Tester": [1.5, 1.4, 1.4], "Camera Tester": [1.6, 1.5, 1.4], "Aging Chamber": [2.2, 2.2, 1.0], "Leak Tester": [1.4, 1.3, 1.4] };
const STATE_TOKEN = { running: "--chart-c4", slowed: "--brand-orange", down: "--depth-0", waiting: "--chart-c3", idle: "--text-muted", no_data: "--text-muted" };
const STATE_LABEL = { running: "Running", slowed: "Slowed", down: "Down", waiting: "Waiting", idle: "Idle", no_data: "No data" };
const PACE = { running: 1, slowed: 0.45, down: 0, waiting: 0, idle: 0, no_data: 0 };
const WH = { "RM-WH-01": ["Raw materials", "--brand-blue"], "SMT-KIT-01": ["SMT kits", "--chart-c3"], "WIP-WH-01": ["Work in progress", "--brand-orange"],
  "FG-WH-01": ["Finished goods", "--chart-c4"] };

// plant-local layout (metres). x: along the lines; z: across. The building's dock wall faces the yard (x = BLD.x0).
// Each plant's building comes from its layout (API): number of bays, hall height, roof style, rack levels, bulk store.
const BAY_Z = [-10.5, -4, 2.5, 9, 15.5];                 // bay rows; bay 1 = recorded SMT, bay 2 = recorded final assembly, then the generated bays
const AISLE_Z = [-13.3, -7.25, -0.75, 5.75, 12.25];      // the aisle beside each bay
const GATE_X = -27, GOODS_X = 47, SPACING = 82;          // staff gate (cars, people) and goods gate (lorries) in the front fence
const WDOCK = { in: [38.5, 43.5], out: [50.5, 55.5] };    // the warehouse's dock doors, on its yard side

function token(name) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return new THREE.Color(v.includes(",") && !v.startsWith("#") ? `rgb(${v})` : v || "gray");
}
const reduced = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
const UP = new THREE.Vector3(0, 1, 0);
const smooth = x => x * x * (3 - 2 * x);
const clamp01 = x => Math.max(0, Math.min(1, x));
function along(pts, s) {                                  // position and direction at fraction s along a polyline
  const segs = pts.slice(1).map((p, i) => p.distanceTo(pts[i])), total = segs.reduce((a, b) => a + b, 0);
  let d = s * total, i = 0;
  while (i < segs.length - 1 && d > segs[i]) { d -= segs[i]; i++; }
  const a = pts[i], b = pts[i + 1];
  return { p: a.clone().lerp(b, segs[i] ? Math.min(1, d / segs[i]) : 0), d: b.clone().sub(a) };
}
function label(text, cls = "") {
  const el = document.createElement("div");
  el.className = "tw3-label " + cls;
  el.textContent = text;
  return new CSS2DObject(el);
}

export default function Floor3D({ snap, plant, sel, onSelect }) {
  const host = useRef();
  const api = useRef(null);

  useEffect(() => {
    const el = host.current;
    const W = () => el.clientWidth, H = () => el.clientHeight;
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    renderer.setPixelRatio(Math.min(2, window.devicePixelRatio));
    renderer.setSize(W(), H());
    el.appendChild(renderer.domElement);
    const labels = new CSS2DRenderer();
    labels.setSize(W(), H());
    labels.domElement.className = "tw3-labels";
    el.appendChild(labels.domElement);

    // this plant's building: a 3-bay hall keeps the base size; more bays make the hall (and the compound) deeper
    const LAY = snap?.plants?.find(p => p.plant_id === plant)?.layout || {};
    const nBays = Math.min(BAY_Z.length, Math.max(2, LAY.bay_lines?.length || 4));
    const BLD = { x0: -16, x1: 28, z0: -15, z1: Math.max(13, BAY_Z[nBays - 1] + 4.5), h: LAY.height_m || 6 };
    const dz = BLD.z1 - 13;
    const COMP = { x0: -38, x1: 66, z0: -20, z1: 22 + dz }, ROAD_Z = 28 + dz;
    const WHB = { x0: 34, x1: 60, z0: BLD.z0, z1: 8, h: BLD.h + 1.5 };              // the warehouse beside the hall: all the stock and the docks
    const CROSS_X = COMP.x0 - 10;                                                   // a side road along the west of the site
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(34, W() / H(), 0.5, 600);
    const HOME = new THREE.Vector3(8, 84 + dz * 0.6, 112 + dz), HOME_T = new THREE.Vector3(12, 0, 3 + dz / 2);      // the whole site of one plant
    scene.fog = new THREE.Fog(0xffffff, 190, 430);
    camera.position.copy(HOME);
    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.maxPolarAngle = 1.38;
    controls.minDistance = 8;
    controls.maxDistance = 300;
    controls.target.copy(HOME_T);

    const mats = [];
    const mat = (tok, { emissiveTok, ...extra } = {}) => {
      const m = new THREE.MeshStandardMaterial({ roughness: 0.75, metalness: 0.08, ...extra });
      mats.push([m, tok, emissiveTok]);
      return m;
    };
    const paint = () => {
      scene.background = token("--page-bg"); scene.fog.color.copy(scene.background);
      mats.forEach(([m, tok, em]) => { m.color.copy(token(tok)); if (em) m.emissive.copy(token(em)); });
    };
    scene.add(new THREE.HemisphereLight(0xffffff, 0x445566, 1.15));
    const sun = new THREE.DirectionalLight(0xffffff, 1.35);
    sun.position.set(30, 60, 25);
    scene.add(sun);

    const box = (w, h, d, m, x, y, z, parent = scene) => {
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m);
      mesh.position.set(x, y, z);
      parent.add(mesh);
      return mesh;
    };
    const cyl = (rt, rb, h, m, x, y, z, parent = scene, seg = 12) => {
      const mesh = new THREE.Mesh(new THREE.CylinderGeometry(rt, rb, h, seg), m);
      mesh.position.set(x, y, z);
      parent.add(mesh);
      return mesh;
    };

    const darkMat = mat("--text-secondary"), frameMat = mat("--text-muted");
    const M = {
      dark: darkMat, frame: frameMat, accent: mat("--brand-blue"), device: mat("--brand-navy"), pcb: mat("--chart-c4", { roughness: 0.5 }),
      glass: mat("--sky", { transparent: true, opacity: 0.3, roughness: 0.1, metalness: 0 }),
      screen: mat("--brand-navy", { emissiveTok: "--brand-blue", emissiveIntensity: 0.3 }),
      heat: mat("--brand-orange", { emissiveTok: "--brand-orange", emissiveIntensity: 0.3 }),
      led: mat("--chart-c4", { emissiveTok: "--chart-c4", emissiveIntensity: 0.8 }),
      red: mat("--depth-0", { emissiveTok: "--depth-0", emissiveIntensity: 0.15 }),
      uniform: mat("--brand-blue"), visitor: mat("--chart-c3"), skin: mat("--surface-hi"),
      safety: mat("--brand-orange", { emissiveTok: "--brand-orange", emissiveIntensity: 0.2 }),
      wood: mat("--chart-c2"), carton: mat("--brand-orange", { roughness: 0.9 }), road: mat("--text-secondary", { roughness: 0.95 }),
      yard: mat("--muted-ink", { roughness: 0.95 }), surface: mat("--surface-hi"), floor: mat("--surface-rgb"), grass: mat("--kg-bg-3"),
      tree: mat("--chart-c4", { roughness: 0.9 }), trunk: mat("--chart-c2"), cabIn: mat("--chart-c3"), agv: mat("--brand-orange"),
      flow: mat("--brand-blue", { emissiveTok: "--brand-blue", emissiveIntensity: 0.5, transparent: true, opacity: 0.85 }),
      aisle: mat("--brand-blue", { transparent: true, opacity: 0.12 }),
    };
    const K = { box: (...a) => box(...a), cyl, M, track: (m, tok, em) => mats.push([m, tok, em]) };

    const ground = new THREE.Mesh(new THREE.PlaneGeometry(900, 640), M.grass);
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -0.03;
    scene.add(ground);
    // public road in front of every plant, with a centre line
    box(900, 0.04, 8, M.road, 0, 0, ROAD_Z);
    for (let x = -440; x < 440; x += 4) box(1.8, 0.05, 0.16, M.surface, x, 0.03, ROAD_Z);
    [-5, 5].forEach(o => box(900, 0.1, 1.8, M.surface, 0, 0.05, ROAD_Z + o));                       // pavements both sides
    box(8, 0.045, ROAD_Z + 200, M.road, CROSS_X, 0.005, (ROAD_Z - 200) / 2 - 4);                    // the side road, north from the main road
    for (let z = ROAD_Z - 8; z > -190; z -= 4) box(0.16, 0.05, 1.8, M.surface, CROSS_X, 0.035, z);
    const smoke = [];                                                                                // chimney smoke puffs (all chimneys)
    const stack = (x, z, h, r = 1, parent = scene) => {                                            // a chimney with two red bands, smoking
      cyl(r * 0.75, r, h, frameMat, x, h / 2, z, parent, 16);
      [0.8, 0.9].forEach(f => cyl(r * 0.8, r * 0.8, h * 0.035, M.red, x, h * f, z, parent, 16));
      for (let i = 0; i < 7; i++) {
        const m = mat("--text-muted", { transparent: true, opacity: 0.35, depthWrite: false });
        const puff = new THREE.Mesh(new THREE.SphereGeometry(r * 0.9, 10, 8), m); parent.add(puff);
        smoke.push({ puff, m, x, z, y: h + 0.4, t: i / 7 });
      }
    };
    const rl0 = label("Public road", "tw3-zone"); rl0.position.set(-SPACING * 0.5, 0.6, ROAD_Z + 5.5); scene.add(rl0);

    const plants = {}, lines = {}, machineMeshes = [], trucks = [], walkers = [], agvs = [], vehicles = [];
    const plantIds = (snap?.plants || []).map(p => p.plant_id).filter(p => p === plant);
    const vehicle = (root, info) => {                               // clickable vehicle, with a ring shown on hover / selection
      const ring = new THREE.Mesh(new THREE.RingGeometry(1.5, 1.8, 32), M.flow);
      ring.rotation.x = -Math.PI / 2; ring.position.y = 0.08; ring.visible = false; root.add(ring);
      root.userData = { ...root.userData, vehicle: info, ring };
      vehicles.push(root);
    };
    // everything else on the site opens its details too: warehouses, docks, packing, the gate, the office, crews, conveyors
    const places = [];
    const hitMat = new THREE.MeshBasicMaterial({ transparent: true, opacity: 0, depthWrite: false });
    const place = (obj, info) => { obj.userData = { ...obj.userData, pick: info }; places.push(obj); return obj; };
    const hit = (w, h, d, x, y, z, info, parent) => {                 // an invisible click area around a group of parts
      const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), hitMat); m.position.set(x, y, z); parent.add(m); return place(m, info);
    };

    plantIds.forEach((plantId, pi) => {
      const P = snap.plants.find(x => x.plant_id === plantId);
      const g = new THREE.Group();
      g.position.x = 0;
      scene.add(g);
      const local = (x, z) => new THREE.Vector3(g.position.x + x, 0, z);     // plant-local -> world

      // ---- compound: yard surface, fence with a gate gap, guard hut, parking, trees ----
      const office = { kind: "area", area: "office", plant: plantId }, gateInfo = { kind: "area", area: "gate", plant: plantId };
      place(box(COMP.x1 - COMP.x0, 0.06, COMP.z1 - COMP.z0, M.yard, (COMP.x0 + COMP.x1) / 2, 0.01, (COMP.z0 + COMP.z1) / 2, g), office);
      box(7, 0.07, ROAD_Z - COMP.z1 - 1, M.road, GATE_X, 0.02, (COMP.z1 + ROAD_Z) / 2 - 0.5, g);      // drive from the road to the gate
      const fence = (x0, z0, x1, z1) => {
        const len = Math.hypot(x1 - x0, z1 - z0), n = Math.ceil(len / 3);
        for (let i = 0; i <= n; i++) box(0.12, 1.6, 0.12, frameMat, x0 + ((x1 - x0) * i) / n, 0.8, z0 + ((z1 - z0) * i) / n, g);
        [0.6, 1.4].forEach(y => { const r = box(len, 0.06, 0.06, frameMat, (x0 + x1) / 2, y, (z0 + z1) / 2, g); r.rotation.y = -Math.atan2(z1 - z0, x1 - x0); });
      };
      fence(COMP.x0, COMP.z0, COMP.x1, COMP.z0); fence(COMP.x0, COMP.z0, COMP.x0, COMP.z1); fence(COMP.x1, COMP.z0, COMP.x1, COMP.z1);
      fence(COMP.x0, COMP.z1, GATE_X - 4, COMP.z1); fence(GATE_X + 4, COMP.z1, GOODS_X - 4, COMP.z1); fence(GOODS_X + 4, COMP.z1, COMP.x1, COMP.z1);
      box(7, 0.07, ROAD_Z - COMP.z1 - 1, M.road, GOODS_X, 0.02, (COMP.z1 + ROAD_Z) / 2 - 0.5, g);       // drive from the road to the goods gate
      const setGate = gate(K, g, GATE_X, COMP.z1, 7), setGoods = gate(K, g, GOODS_X, COMP.z1, 7);
      hit(9, 3.4, 3, GATE_X, 1.7, COMP.z1, gateInfo, g); hit(9, 3.4, 3, GOODS_X, 1.7, COMP.z1, gateInfo, g);
      const gl = label(`Staff gate · ${plantId}`, "tw3-zone tw3-gate"); gl.position.set(GATE_X, 3.2, COMP.z1 + 1); g.add(gl);
      const gg = label("Goods gate", "tw3-zone tw3-gate"); gg.position.set(GOODS_X, 3.2, COMP.z1 + 1); g.add(gg);
      // staff car park (its own lot west of the hall): an aisle from the staff gate, bays both sides, most of them taken
      const LOT = { x0: COMP.x0 + 1, x1: BLD.x0 - 3, z0: COMP.z0 + 2, z1: COMP.z1 - 4 }, bayZ = [];
      place(box(LOT.x1 - LOT.x0, 0.05, LOT.z1 - LOT.z0, M.road, (LOT.x0 + LOT.x1) / 2, 0.03, (LOT.z0 + LOT.z1) / 2, g), gateInfo);
      for (let z = LOT.z0 + 1.6; z < LOT.z1 - 1.2; z += 2.8) bayZ.push(z);
      const bayX = [GATE_X - 5.2, GATE_X + 5.2];
      bayZ.forEach(z => bayX.forEach(x => box(4.6, 0.02, 0.08, M.surface, x, 0.07, z - 1.4, g)));       // painted bay lines
      [-1, 1].forEach(sd => box(0.1, 0.02, LOT.z1 - LOT.z0 - 2, M.surface, GATE_X + sd * 2.9, 0.07, (LOT.z0 + LOT.z1) / 2, g));
      const carMats = [M.accent, M.cabIn, M.visitor, M.agv, M.surface, darkMat, M.device, M.red];
      const moving = [[0, 2], [1, 5], [0, 8]];                                          // bays kept free for the cars that come and go
      bayZ.forEach((z, zi) => bayX.forEach((x, xi) => {
        if (moving.some(([mx, mz]) => mx === xi && mz === zi) || (zi * 3 + xi * 5) % 7 === 0) return;      // a few empty bays
        const c = car(K, carMats[(zi * 2 + xi * 3) % carMats.length]); c.position.set(x, 0, z); c.rotation.y = xi ? Math.PI : 0; g.add(c);
      }));
      const pk = label("Staff car park", "tw3-zone"); pk.position.set(GATE_X, 1.6, LOT.z0 + 1); g.add(pk);
      const lotCars = moving.map(([xi, zi], i) => {
        const c = car(K, carMats[(i * 3 + 1) % carMats.length]); scene.add(c);
        const A = local(GATE_X, bayZ[zi]), B = local(bayX[xi], bayZ[zi]);
        return { c, ph: (i * 0.33 + 0.1) % 1, dur: 46 + i * 7, pts: [local(GATE_X - 140, ROAD_Z + 1.8), local(GATE_X, ROAD_Z + 1.8), local(GATE_X, COMP.z1 - 2), A, B,
          local(GATE_X, ROAD_Z - 1.8), local(GATE_X - 140, ROAD_Z - 1.8)] };
      });
      const inner = [pk];                                                              // labels shown only once you zoom in
      for (let i = 0; i < Math.floor((COMP.z1 - COMP.z0) / 5) - 1; i++) [[COMP.x0 - 3, COMP.z0 + 4 + i * 5], [COMP.x1 + 3, COMP.z0 + 4 + i * 5]].forEach(([tx, tz]) => {
        cyl(0.15, 0.2, 1.4, M.trunk, tx, 0.7, tz, g, 6);
        const c = new THREE.Mesh(new THREE.ConeGeometry(1.2, 2.6, 8), M.tree); c.position.set(tx, 2.6, tz); g.add(c);
      });

      // ---- the building: floor, walls (fade when zoomed in), dock doors, roof with skylights, office ----
      const bw = BLD.x1 - BLD.x0, bd = BLD.z1 - BLD.z0, bcx = (BLD.x0 + BLD.x1) / 2, bcz = (BLD.z0 + BLD.z1) / 2;
      place(box(bw, 0.12, bd, M.floor, bcx, 0.06, bcz, g), office);
      const wallMat = mat("--surface-hi", { transparent: true, opacity: 1 }), roofMat = mat("--text-muted", { transparent: true, opacity: 1 });
      const skyMat = mat("--sky", { transparent: true, opacity: 1, emissiveTok: "--sky", emissiveIntensity: 0.15 });
      const walls = [];
      const wall = (w, d, x, z) => walls.push(box(w, BLD.h, d, wallMat, x, BLD.h / 2, z, g));
      wall(bw, 0.3, bcx, BLD.z0); wall(bw, 0.3, bcx, BLD.z1); wall(0.3, bd, BLD.x1, bcz);
      wall(0.3, bd, BLD.x0, bcz);                                       // goods come and go through the warehouse: the hall has no dock doors
      place(box(2.4, 2.8, 0.12, M.safety, -10, 1.4, BLD.z1 + 0.2, g), office);                   // staff entrance
      const se = label("Staff entrance", "tw3-zone"); se.position.set(-10, 3.6, BLD.z1 + 0.8); g.add(se);
      // roof by plant: a pitched (gable) roof with a ridge vent, a sawtooth roof with north-light glazing, or flat with skylights
      const roof = new THREE.Group(); g.add(roof);
      const skylights = [];
      let rise = 0;
      if (LAY.roof === "gable") {
        const a = 0.2, half = bd / 2 + 0.3, w = half / Math.cos(a);
        rise = Math.tan(a) * half;
        [-1, 1].forEach(s => { box(bw + 0.8, 0.25, w, roofMat, bcx, BLD.h + rise / 2, bcz + (s * half) / 2, roof).rotation.x = s * a; });
        skylights.push(box(bw, 0.3, 1.2, skyMat, bcx, BLD.h + rise + 0.1, bcz, roof));
        const tri = new THREE.Shape(); tri.moveTo(-bd / 2, 0); tri.lineTo(bd / 2, 0); tri.lineTo(0, rise); tri.closePath();
        [BLD.x0, BLD.x1].forEach(gx => { const m = new THREE.Mesh(new THREE.ShapeGeometry(tri), wallMat); m.material.side = THREE.DoubleSide;
          m.rotation.y = Math.PI / 2; m.position.set(gx, BLD.h, bcz); roof.add(m); });
      } else if (LAY.roof === "sawtooth") {
        const n = Math.max(3, Math.round(bd / 6)), tw = (bd + 0.6) / n;
        rise = 1.8;
        const ang = Math.atan2(rise, tw), L = Math.hypot(tw, rise);
        for (let i = 0; i < n; i++) {
          const zs = BLD.z0 - 0.3 + i * tw;
          box(bw + 0.6, 0.2, L, roofMat, bcx, BLD.h + rise / 2, zs + tw / 2, roof).rotation.x = -ang;
          skylights.push(box(bw + 0.6, rise, 0.1, skyMat, bcx, BLD.h + rise / 2, zs + tw, roof));
        }
      } else {
        box(bw + 0.6, 0.25, bd + 0.6, roofMat, bcx, BLD.h + 0.12, bcz, roof);
        skylights.push(...[-8, 0, 8, 16].map(sx => box(3, 0.1, bd - 4, skyMat, sx, BLD.h + 0.3, bcz, roof)));
      }
      const edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(bw, BLD.h, bd)), new THREE.LineBasicMaterial({ transparent: true }));
      edges.position.set(bcx, BLD.h / 2, bcz); g.add(edges); mats.push([edges.material, "--text-secondary"]);
      const roofLabel = label(`Plant ${plantId}`, "tw3-hall"); roofLabel.position.set(bcx, BLD.h + rise + 1.5, bcz); g.add(roofLabel);
      // office block in the corner, with windows
      place(box(4.4, 3.2, 2.6, M.surface, BLD.x1 - 2.4, 1.6, BLD.z1 - 1.5, g), office);
      for (let i = 0; i < 3; i++) box(1.0, 0.9, 0.04, M.glass, BLD.x1 - 3.8 + i * 1.4, 2, BLD.z1 - 2.83, g);
      const of = label("Office", "tw3-zone"); of.position.set(BLD.x1 - 2.4, 3.8, BLD.z1 - 1.5); g.add(of); inner.push(of);
      // painted aisles
      [...AISLE_Z.slice(0, nBays).map(z => [bcx + 2, z, bw - 6, 1.2]), [-3.2, bcz, 1.2, bd - 2]]
        .forEach(([x, z, w, d]) => box(w, 0.02, d, M.aisle, x, 0.13, z, g));

      // ---- the rest of the production hall (illustrative): west end beside the bays, columns, lights, duct, KPI screen, trolleys ----
      // West end (x -15.5 .. -7) is clear of the walkway at x -6 .. -3.2, the packing table and the material route.
      const WX = -11.5, zEnd = BLD.z1 - 0.8;
      const area = (t, x, z) => { const a = label(t, "tw3-zone tw3-area"); a.position.set(x, 0.3, z); g.add(a); inner.push(a); };
      const table = (x, z, w, d, top = M.surface) => {                                   // a bench: top and four legs
        box(w, 0.08, d, top, x, 0.9, z, g);
        [[-1, -1], [1, -1], [-1, 1], [1, 1]].forEach(([a, b]) => box(0.06, 0.9, 0.06, darkMat, x + a * (w / 2 - 0.08), 0.45, z + b * (d / 2 - 0.08), g));
      };
      const screen = (x, z, face = 0) => { const m = box(0.55, 0.36, 0.04, M.screen, x, 1.28, z, g); m.rotation.y = face; box(0.05, 0.3, 0.05, darkMat, x, 1.05, z, g); };
      // quality lab: a glass room with inspection benches and a microscope station
      const qz0 = BLD.z0 + 0.6, qz1 = qz0 + 5.6, qx0 = BLD.x0 + 0.6, qx1 = -7.4;
      box(qx1 - qx0, 0.03, qz1 - qz0, M.aisle, (qx0 + qx1) / 2, 0.14, (qz0 + qz1) / 2, g);
      [[qx0, qz1, qx1, qz1], [qx1, qz0, qx1, qz1]].forEach(([x0, z0, x1, z1]) => {
        const len = Math.hypot(x1 - x0, z1 - z0), gl2 = box(len, 2.6, 0.06, M.glass, (x0 + x1) / 2, 1.3, (z0 + z1) / 2, g);
        gl2.rotation.y = -Math.atan2(z1 - z0, x1 - x0);
        const tr = box(len, 0.08, 0.1, frameMat, (x0 + x1) / 2, 2.62, (z0 + z1) / 2, g); tr.rotation.y = gl2.rotation.y;
      });
      table(-12.6, qz0 + 1.3, 3.6, 0.9); table(-12.6, qz0 + 4.1, 3.6, 0.9);
      [-13.8, -11.6].forEach(x => { box(0.5, 0.45, 0.4, M.device, x, 1.17, qz0 + 1.3, g); cyl(0.06, 0.06, 0.4, darkMat, x + 0.1, 1.5, qz0 + 1.3, g, 8); });   // microscopes
      screen(-12.6, qz0 + 3.75); box(1.0, 0.5, 0.7, M.accent, -10.6, 1.2, qz0 + 4.1, g);                                  // analyser
      operator(K, g, -12.6, qz0 + 2.15, Math.PI / 2); operator(K, g, -11.2, qz0 + 3.3, -Math.PI / 2);
      area("Quality lab", WX, qz1 + 0.6);
      // rework and repair benches (ESD), with screens and stools
      const rz = qz1 + 2.2;
      [0, 1].forEach(i => {
        const z = rz + i * 2.2; table(WX, z, 4.6, 0.9, M.pcb);
        [-1.4, 0, 1.4].forEach(o => { screen(WX + o, z - 0.3); cyl(0.22, 0.22, 0.5, darkMat, WX + o, 0.25, z + 0.75, g, 10); });
        box(0.6, 0.04, 0.4, M.pcb, WX - 0.7, 0.96, z + 0.1, g);
      });
      operator(K, g, WX - 0.7, rz + 0.8, Math.PI / 2);
      area("Rework benches", WX, rz + 3.6);
      // line-side supermarket: flow racks with coloured parts bins, refilled from the warehouse
      const sz = rz + 5.2;
      if (sz + 2 < zEnd) {
        [-13.6, -9.4].forEach(x => {
          [-0.7, 0.7].forEach(o => box(0.06, 1.9, 0.06, darkMat, x + o * 2, 0.95, sz, g));
          for (let lv = 0; lv < 3; lv++) {
            const sh = box(2.9, 0.05, 0.9, frameMat, x, 0.45 + lv * 0.6, sz, g); sh.rotation.x = -0.12;
            for (let b = 0; b < 5; b++) box(0.46, 0.3, 0.6, [M.accent, M.safety, M.pcb, M.cabIn, M.red][(b + lv) % 5], x - 1.1 + b * 0.55, 0.65 + lv * 0.6, sz, g);
          }
        });
        area("Line-side parts", WX, sz + 1.2);
      }
      // maintenance workshop: a caged area with a workbench, tool cabinet, spares shelf and a pedestal drill
      const mz0 = sz + 2.6, mz1 = Math.min(zEnd - 1.4, mz0 + 5.2);
      if (mz1 - mz0 > 3) {
        const cage = (x0, z0, x1, z1) => { const len = Math.hypot(x1 - x0, z1 - z0), n = Math.ceil(len / 1.4);
          for (let i = 0; i <= n; i++) box(0.06, 2.0, 0.06, frameMat, x0 + ((x1 - x0) * i) / n, 1.0, z0 + ((z1 - z0) * i) / n, g);
          [0.3, 1.0, 1.9].forEach(y => { const r = box(len, 0.04, 0.04, frameMat, (x0 + x1) / 2, y, (z0 + z1) / 2, g); r.rotation.y = -Math.atan2(z1 - z0, x1 - x0); }); };
        cage(qx0, mz0, qx1, mz0); cage(qx1, mz0, qx1, mz1 - 1.6);
        table(-12.8, mz0 + 1.2, 3.2, 1.0, M.wood);
        box(0.9, 1.9, 0.6, M.red, -9, 0.95, mz0 + 0.8, g);                                   // tool cabinet
        box(2.6, 1.8, 0.6, frameMat, -12.8, 0.9, mz1 - 0.4, g);                              // spares shelf
        for (let i = 0; i < 4; i++) box(0.5, 0.35, 0.45, M.carton, -13.8 + i * 0.66, 1.1 + (i % 2) * 0.6, mz1 - 0.4, g);
        cyl(0.12, 0.25, 1.6, darkMat, -9.4, 0.8, mz1 - 1.4, g, 10); box(0.5, 0.4, 0.6, M.safety, -9.4, 1.7, mz1 - 1.4, g);   // pedestal drill
        operator(K, g, -12.8, mz0 + 2.0, -Math.PI / 2);
        area("Maintenance", WX, mz0 + 2.6);
      }
      // lockers beside the staff entrance
      for (let i = 0; i < 6; i++) box(0.55, 1.9, 0.5, [M.accent, M.device][i % 2], -15.2 + i * 0.6, 0.95, BLD.z1 - 0.45, g);
      // steel columns along the walls, hanging lights over the bays, an air duct, a KPI screen
      for (let x = BLD.x0 + 4; x < BLD.x1 - 1; x += 6) [BLD.z0 + 0.35, BLD.z1 - 0.35].forEach(z => walls.push(box(0.35, BLD.h, 0.35, wallMat, x, BLD.h / 2, z, g)));   // fade with the walls
      const lamp = mat("--surface-hi", { emissiveTok: "--surface-hi", emissiveIntensity: 0.6 });
      BAY_Z.slice(0, nBays).forEach(z => { for (let x = 2; x < 23; x += 9) { box(1.1, 0.06, 0.24, lamp, x, BLD.h - 0.9, z, g); box(0.03, 0.8, 0.03, darkMat, x, BLD.h - 0.5, z, g); } });
      const duct = cyl(0.45, 0.45, bw - 4, frameMat, bcx, BLD.h - 0.6, BLD.z0 + 2.2, g, 14); duct.rotation.z = Math.PI / 2;
      box(6, 2.2, 0.12, darkMat, 10, 3.6, BLD.z0 + 0.3, g); box(5.6, 1.8, 0.04, M.screen, 10, 3.6, BLD.z0 + 0.38, g);
      area("Hall dashboard", 10, BLD.z0 + 1.4);
      // trolleys of work in progress waiting at the line ends
      BAY_Z.slice(0, nBays).forEach((z, i) => { if (i % 2) return;
        const tx = 24.6, tz = z + 1.6; box(1.2, 0.06, 0.8, frameMat, tx, 0.7, tz, g); box(1.2, 0.06, 0.8, frameMat, tx, 0.25, tz, g);
        [[-0.55, -0.35], [0.55, -0.35], [-0.55, 0.35], [0.55, 0.35]].forEach(([a, b]) => box(0.04, 0.9, 0.04, darkMat, tx + a, 0.45, tz + b, g));
        [-0.3, 0.3].forEach(o => box(0.5, 0.3, 0.6, M.carton, tx + o, 0.9, tz, g)); });

      // ---- the warehouse: a separate building beside the hall with all the stock and the dock doors; bridges to the hall; a chimney ----
      const ww = WHB.x1 - WHB.x0, wd = WHB.z1 - WHB.z0, wcx = (WHB.x0 + WHB.x1) / 2, wcz = (WHB.z0 + WHB.z1) / 2;
      const whInfo = { kind: "warehouse", wh: "RM-WH-01", plant: plantId }, whInner = [];
      place(box(ww, 0.12, wd, M.floor, wcx, 0.06, wcz, g), whInfo);
      const whWall = mat("--surface-hi", { transparent: true, opacity: 1 }), whRoofMat = mat("--text-secondary", { transparent: true, opacity: 1 });
      const wwall = (w, d, x, z, h = WHB.h, y = h / 2) => box(w, h, d, whWall, x, y, z, g);
      wwall(ww, 0.3, wcx, WHB.z0); wwall(0.3, wd, WHB.x0, wcz); wwall(0.3, wd, WHB.x1, wcz);
      const wdoors = [...WDOCK.in, ...WDOCK.out];
      let xc = WHB.x0;
      wdoors.forEach(dx => { wwall(dx - 1.7 - xc, 0.3, (xc + dx - 1.7) / 2, WHB.z1); xc = dx + 1.7; });
      wwall(WHB.x1 - xc, 0.3, (xc + WHB.x1) / 2, WHB.z1);
      wdoors.forEach(dx => {
        wwall(3.4, 0.32, dx, WHB.z1, WHB.h - 4, 4 + (WHB.h - 4) / 2);                             // lintel
        const dock = { kind: "area", area: WDOCK.in.includes(dx) ? "receiving" : "shipping", plant: plantId };
        place(box(3.6, 0.3, 0.5, M.safety, dx, 4.1, WHB.z1 + 0.2, g), dock);                      // door frame
        place(box(3.2, 0.9, 2.4, darkMat, dx, 0.45, WHB.z1 + 1.2, g), dock);                      // dock leveller
        const dl = label(WDOCK.in.includes(dx) ? "Receiving dock" : "Shipping dock", "tw3-zone tw3-door"); dl.position.set(dx, 5, WHB.z1 + 1.5); g.add(dl); whInner.push(dl);
      });
      const whRoof = new THREE.Group(); g.add(whRoof);
      box(ww + 0.6, 0.25, wd + 0.6, whRoofMat, wcx, WHB.h + 0.12, wcz, whRoof);
      [-6, 0, 6].forEach(o => box(ww - 4, 0.6, 1.2, whRoofMat, wcx, WHB.h + 0.5, wcz + o, whRoof));           // roof vents
      const whEdges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(ww, WHB.h, wd)), new THREE.LineBasicMaterial({ transparent: true }));
      whEdges.position.set(wcx, WHB.h / 2, wcz); g.add(whEdges); mats.push([whEdges.material, "--text-secondary"]);
      const whLabel = label("Warehouse · all stock", "tw3-hall"); whLabel.position.set(wcx, WHB.h + 2, wcz); g.add(whLabel);
      // covered bridges: kits go to the lines, finished goods come back
      [[-13.3, "Kits to the lines"], [5.75, "Finished goods to the warehouse"]].forEach(([bz, t]) => {
        place(box(WHB.x0 - BLD.x1, 2.6, 2.4, M.surface, (BLD.x1 + WHB.x0) / 2, 2.4, bz, g), whInfo);
        box(WHB.x0 - BLD.x1 + 0.02, 0.6, 2.42, M.glass, (BLD.x1 + WHB.x0) / 2, 2.9, bz, g);
        const bl2 = label(t, "tw3-zone"); bl2.position.set((BLD.x1 + WHB.x0) / 2, 4.4, bz); g.add(bl2); inner.push(bl2);
      });
      // the chimney on the warehouse: a duct from the hall (reflow ovens) carries the fumes out
      const chx = WHB.x0 - 1.3, chz = -4;                                     // against the warehouse wall, between the two bridges
      stack(chx, chz, WHB.h + 16, 1.1, g);
      box(chx - BLD.x1, 0.9, 0.9, frameMat, (BLD.x1 + chx) / 2, BLD.h + 0.4, chz, g);              // duct from the hall roof
      const ch = label("Chimney · fumes from the ovens", "tw3-zone"); ch.position.set(chx, WHB.h + 18, chz); g.add(ch); inner.push(ch);

      // ---- line bays in the plant's order: 1-2 the recorded lines, then the generated twin-only lines (Hosur 3 bays, Bengaluru 5) ----
      const pl = snap.lines.filter(l => l.plant_id === plantId), pick2 = (k, gen) => pl.find(l => l.kind === k && !!l.generated === gen);
      const real = { SMT: pick2("SMT", false), FAL: pick2("FAL", false) };
      const order = LAY.bay_lines || pl.map(l => l.line_id);
      const bayDefs = order.slice(0, nBays).map((id, bi) => { const L0 = pl.find(l => l.line_id === id); return [L0, L0?.kind, bi]; });
      bayDefs.forEach(([L0, kind, bi]) => {
        const template = real[kind];
        if (!template || !L0) return;
        const record = !!L0;
        const L = L0 || { line_id: `${plantId}-bay${bi + 1}`, kind, state: "no_data", machines: template.machines.map(m => ({ ...m, machine_id: `${m.machine_id}-b${bi + 1}`, state: "no_data" })) };
        const lineId = L.line_id, smt = kind === "SMT", z = BAY_Z[bi], dir = smt ? 1 : -1;
        let x = smt ? -1 : 20.5;
        const start = x;
        const ms = [], anims = [], crew = [];
        const walk = smt ? 1.6 : -1.6;
        L.machines.forEach((m, mi) => {
          const [w, h, d] = SIZE[m.type] || [1.6, 1.3, 1.4];
          const cx = x + dir * w / 2;
          const mg = new THREE.Group(); mg.position.set(cx, 0, z); if (!smt) mg.rotation.y = Math.PI; g.add(mg);
          const body = box(w, h, d, M.surface.clone(), 0, h / 2 + 0.1, 0, mg);
          mats.push([body.material, "--surface-hi"]);
          const mState = m.state || L.state;                         // each machine shows its own state (its own record)
          const mBand = mat(STATE_TOKEN[mState] || STATE_TOKEN.no_data, { emissiveTok: STATE_TOKEN[mState] || STATE_TOKEN.no_data, emissiveIntensity: 0.35 });
          const mBeacon = mat(STATE_TOKEN[mState] || STATE_TOKEN.no_data, { emissiveTok: STATE_TOKEN[mState] || STATE_TOKEN.no_data, emissiveIntensity: 0.9 });
          box(w + 0.02, 0.14, d + 0.02, mBand, 0, h * 0.72, 0, mg);
          box(w + 0.1, 0.1, d + 0.1, darkMat, 0, 0.05, 0, mg);
          anims.push(...buildMachine(K, m.type, mg, w, h, d));
          if (record && (!smt || mi === 0)) crew.push(place(operator(K, g, cx, z + walk * (d / 2 + 0.55) / 1.6, smt ? Math.PI : 0), { kind: "crew", line: lineId }));
          if (m.type === "Assembly Cell") {
            const e = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(w + 0.3, h + 0.4, d + 0.3)), new THREE.LineBasicMaterial());
            e.position.y = (h + 0.4) / 2 + 0.1; mg.add(e); mats.push([e.material, "--text-muted"]);
          }
          box(0.06, 0.7, 0.06, darkMat, w / 2 - 0.25, h + 0.45, d / 2 - 0.25, mg);
          const beacon = new THREE.Mesh(new THREE.SphereGeometry(0.17, 16, 12), mBeacon);
          beacon.position.set(w / 2 - 0.25, h + 0.85, d / 2 - 0.25); mg.add(beacon);
          const tag = label(m.type, "tw3-mach"); tag.position.set(0, h + 1.4, 0); tag.visible = false; mg.add(tag);
          mg.userData = { machine: m.machine_id, line: lineId, body, tag, modelled: !record, type: m.type };
          body.userData = { group: mg };
          machineMeshes.push(body);
          ms.push({ mg, beacon, tag, id: m.machine_id, band: mBand, beaconMat: mBeacon, state: mState });
          x += dir * (w + 1.4);
        });
        const end = x - dir * 1.4, len = Math.abs(end - start) + 3, mid = (start + end) / 2;
        const lineInfo = { kind: "line", id: lineId };
        place(box(len, 0.12, 0.7, darkMat, mid, 0.55, z, g), lineInfo);
        [-0.42, 0.42].forEach(o => box(len, 0.08, 0.06, frameMat, mid, 0.66, z + o, g));
        for (let s = 0; s < len; s += 2.2) [-0.32, 0.32].forEach(o => box(0.06, 0.5, 0.06, darkMat, mid - len / 2 + s, 0.25, z + o, g));
        [-1, 1].forEach(s => box(len + 1, 0.02, 0.08, M.safety, mid, 0.135, z + s * 2.0, g));
        const N = record ? Math.round(len / 1.3) : 0;
        const boards = new THREE.InstancedMesh(smt ? new THREE.BoxGeometry(0.5, 0.04, 0.36) : new THREE.BoxGeometry(0.3, 0.08, 0.52), smt ? M.pcb : M.device, Math.max(N, 1));
        const chips = smt ? new THREE.InstancedMesh(new THREE.BoxGeometry(0.16, 0.04, 0.12), darkMat, Math.max(N, 1)) : null;
        boards.count = N; if (chips) chips.count = N;
        g.add(boards); if (chips) g.add(chips);
        const sx = start - dir * 1.6;
        const setAndon = andon(K, g, sx, z - walk * 0.9);
        box(1.0, 0.7, 0.6, frameMat, sx - dir * 0.2, 0.45, z + walk * 0.9, g);
        const ex = end + dir * 0.9;
        box(0.8, 0.06, 0.8, darkMat, ex, 0.05, z + walk * 1.0, g);
        [[-0.4, 0], [0.4, 0], [0, -0.4], [0, 0.4]].forEach(([ox, oz]) => box(ox ? 0.04 : 0.8, 0.6, ox ? 0.8 : 0.04, M.red, ex + ox, 0.35, z + walk * 1.0 + oz, g));
        const rejectFill = box(0.72, 1, 0.72, M.red, ex, 0.1, z + walk * 1.0, g);
        rejectFill.scale.y = 0.02;
        hit(1.2, 1, 1.2, ex, 0.4, z + walk * 1.0, lineInfo, g);                  // reject bin and the andon belong to the line
        hit(1.2, 2.6, 1.2, sx, 1.3, z - walk * 0.9, lineInfo, g);
        const ll = label(lineId, "tw3-line tw3-board" + (record ? "" : " tw3-norec"));
        ll.position.set(sx - dir * 0.6, 2.2, z - walk * 1.6); g.add(ll);
        const bay = label(`Bay ${bi + 1}`, "tw3-zone tw3-bay"); bay.position.set(dir > 0 ? start - 3.4 : start + 3.4, 0.3, z); g.add(bay); inner.push(bay);
        lines[lineId] = { L, record, plant: plantId, bay: bi + 1, boards, chips, N, len, mid, z, dir, offset: 0, phase: 0, ms, anims, crew,
          setAndon, rejectFill, label: ll, near: null, center: local(mid, z) };
      });

      // ---- stores, WIP, finished goods, staging, packing, forklifts, AGVs ----
      const racks = [];
      const rack = (wh, rx, rz, vertical, title) => {
        const rg = new THREE.Group(); rg.position.set(rx, 0, rz); g.add(rg);
        place(rg, { kind: "warehouse", wh, plant: plantId });
        // Hosur: low selective racks (3 levels); Bengaluru: high-bay racks (6 levels, blue uprights with cross-bracing)
        const Lx = vertical ? 1.6 : 6, Dz = vertical ? 5 : 1.6, levels = LAY.rack_levels || 4, slots = 5, ph = levels * 1.1 + 0.4, high = levels >= 6;
        [[-Lx / 2, -Dz / 2], [Lx / 2, -Dz / 2], [-Lx / 2, Dz / 2], [Lx / 2, Dz / 2]].forEach(([px, pz]) => box(0.12, ph, 0.12, high ? M.accent : darkMat, px, ph / 2, pz, rg));
        if (high) [-1, 1].forEach(s => { const br = box(0.05, Math.hypot(ph, vertical ? Dz : Lx), 0.05, M.accent, vertical ? s * Lx / 2 : 0, ph / 2, vertical ? 0 : s * Dz / 2, rg);
          br.rotation[vertical ? "x" : "z"] = Math.atan2(vertical ? Dz : Lx, ph) * s; });
        for (let lv = 0; lv < levels; lv++) box(Lx, 0.08, Dz, frameMat, 0, 0.3 + lv * 1.1, 0, rg);
        const cm = mat(WH[wh][1]), crates = [];
        for (let lv = 0; lv < levels; lv++) for (let s = 0; s < slots; s++) {
          const t = (s + 0.5) / slots - 0.5;
          crates.push(box(vertical ? 1.1 : Lx / slots * 0.8, 0.7, vertical ? Dz / slots * 0.8 : 1.1, cm, vertical ? 0 : t * Lx, 0.72 + lv * 1.1, vertical ? t * Dz : 0, rg));
        }
        const wl = label(title || WH[wh][0], "tw3-zone"); wl.position.set(0, ph + 0.6, 0); rg.add(wl);
        racks.push({ plant: plantId, wh, crates, label: wl, title: title || WH[wh][0] });
      };
      rack("RM-WH-01", 39, -12.4, false); rack("SMT-KIT-01", 39, -7.6, false); rack("WIP-WH-01", 54.6, -12.4, false); rack("FG-WH-01", 54.6, -7.6, false);
      if (LAY.bulk_store) {                                     // Hosur: raw materials also floor-stacked in the hall beyond the last bay
        const bg = new THREE.Group(); bg.position.set(wcx, 0, -1.6); bg.scale.set(0.95, 1, 0.8); g.add(bg);
        place(bg, { kind: "warehouse", wh: "RM-WH-01", plant: plantId });
        [-1, 1].forEach(s => { box(22, 0.02, 0.1, M.safety, 0, 0.135, s * 2.6, bg); box(0.1, 0.02, 5.2, M.safety, s * 11, 0.135, 0, bg); });
        const cm = mat(WH["RM-WH-01"][1]), crates = [];
        for (let c2 = 0; c2 < 9; c2++) for (let r = 0; r < 3; r++) {
          const px = -9.6 + c2 * 2.4, pz = -1.5 + r * 1.5;
          box(1.1, 0.14, 1.1, M.wood, px, 0.19, pz, bg);
          for (let lv = 0; lv < 2; lv++) crates.push(box(1.0, 0.8, 1.0, cm, px, 0.67 + lv * 0.82, pz, bg));
        }
        const bl = label("Bulk store · raw materials", "tw3-zone"); bl.position.set(0, 2.6, 0); bg.add(bl);
        racks.push({ plant: plantId, wh: "RM-WH-01", crates, label: bl, title: "Bulk store · raw materials" });
      }
      const recv = label("Receiving & stores", "tw3-zone tw3-area"); recv.position.set(41, 0.3, 3); g.add(recv);
      const ship = label("Dispatch staging", "tw3-zone tw3-area"); ship.position.set(53, 0.3, 1.2); g.add(ship); whInner.push(recv, ship);
      const pkl = label("Packing", "tw3-zone tw3-area"); pkl.position.set(-5, 0.3, 1.4); g.add(pkl); inner.push(pkl);
      const packAnim = packing(K, g, -5, 3.6);
      hit(3.6, 2.4, 3.2, -5, 1.2, 3.6, { kind: "area", area: "packing", plant: plantId }, g);
      const staging = [];
      for (let i = 0; i < 9; i++) {
        const pg = new THREE.Group(); pg.position.set(50.4 + (i % 3) * 1.25, 0, 3.2 + Math.floor(i / 3) * 1.15); g.add(pg);
        place(pg, { kind: "warehouse", wh: "FG-WH-01", plant: plantId });
        box(1.1, 0.14, 0.95, M.wood, 0, 0.08, 0, pg); box(1.0, 0.75, 0.85, M.carton, 0, 0.55, 0, pg);
        staging.push(pg);
      }
      const fkIn = forklift(K), fkOut = forklift(K); g.add(fkIn.f); g.add(fkOut.f);
      vehicle(fkIn.f, { id: `${plantId}-FL-1`, type: "forklift", role: "receiving", plant: plantId, name: "Forklift 1 · receiving" });
      vehicle(fkOut.f, { id: `${plantId}-FL-2`, type: "forklift", role: "shipping", plant: plantId, name: "Forklift 2 · shipping" });
      const smtL = lines[real.SMT?.line_id], falL = lines[real.FAL?.line_id];
      [[smtL, [[26.5, -13.3], [-1.5, -13.3]]], [falL, [[-1.5, -2.4], [-4.2, 2.2]]]].forEach(([line, path]) => {
        if (!line) return;
        const v = new THREE.Group(); box(1.3, 0.35, 0.9, M.agv, 0, 0.3, 0, v); box(0.8, 0.35, 0.8, M.accent, 0, 0.65, 0, v);
        g.add(v); agvs.push({ v, line, path, t: Math.random() });
        const n = line === smtL ? 1 : 2;
        vehicle(v, { id: `${plantId}-AGV-${n}`, type: "agv", plant: plantId, line: line.L.line_id, name: n === 1 ? "AGV 1 · kits to SMT" : "AGV 2 · products to packing" });
      });
      // material route and numbered steps (same seven steps as the strip above the view)
      const route = new THREE.CatmullRomCurve3([[41, 11], [41, 4], [36.4, -2], [36.4, -10.2], [33, -13.3], [24, -13.3], [-1.5, -13.3], [-1.5, -11.9],
        [23, -11.9], [27, -10], [27, -4.5], [23, -0.75], [-1.5, -0.75], [-4.6, 2.4], [-4.6, 5.75], [28, 5.75], [36, 5.75], [50, 5], [53, 11]].map(([x2, z2]) => new THREE.Vector3(x2, 0.15, z2)), false, "catmullrom", 0.2);
      const antN = Math.round(route.getLength() / 1.1);
      const ants = new THREE.InstancedMesh(new THREE.ConeGeometry(0.16, 0.42, 3), M.flow, antN); g.add(ants);
      const steps = [["1", "Inbound", 41, 12.5], ["2", "Inventory", 39, -10], ["3", "Issued to line", 31, -13.3], ["4", "SMT", 10, -11.9],
        ["5", "Assembly", 10, -0.75], ["6", "Logistics", -7.5, 3.4], ["7", "Dispatch", 53, 12.5]].map(([n, t, sx2, sz]) => {
        const e2 = document.createElement("button"); e2.type = "button"; e2.className = "tw3-step";
        const b = document.createElement("b"); b.textContent = n; const s = document.createElement("span"); s.textContent = t;
        e2.append(b, s);
        const to = { 1: { kind: "area", area: "receiving", plant: plantId }, 2: { kind: "warehouse", wh: "RM-WH-01", plant: plantId },
          3: { kind: "warehouse", wh: "SMT-KIT-01", plant: plantId }, 4: { kind: "line", id: real.SMT?.line_id }, 5: { kind: "line", id: real.FAL?.line_id },
          6: { kind: "area", area: "packing", plant: plantId }, 7: { kind: "area", area: "shipping", plant: plantId } }[n];
        e2.setAttribute("aria-label", `Step ${n}: ${t}`);
        e2.addEventListener("click", () => api.current?.onSelect(to));
        const o = new CSS2DObject(e2); o.position.set(sx2, 0.4, sz); g.add(o);
        return e2;
      });

      // ---- people: inside the aisles (hard hats) and outside between parking, the entrance and the gate ----
      const loop = pts => new THREE.CatmullRomCurve3(pts.map(([x2, z2]) => new THREE.Vector3(x2, 0, z2)), true, "catmullrom", 0.1);
      const inside = [loop([[-2.5, -13.3], [24, -13.3], [24, -7.25], [-2.5, -7.25]]), loop([[-2.5, -0.75], [24, -0.75], [24, 5.75], [-2.5, 5.75]]),
        loop([[-3.2, -13.5], [-3.2, BLD.z1 - 1.5], [-6, BLD.z1 - 1.5], [-6, -13.5]]), loop([[35.2, -10], [58.8, -10], [58.8, 1.8], [35.2, 1.8]])];
      const entr = BLD.z1 + 1.4;
      const outside = [loop([[GATE_X + 2.2, LOT.z0 + 2], [GATE_X + 2.2, entr - 0.3], [-10.6, entr - 0.3], [-10.6, entr + 0.3], [GATE_X + 2.6, entr + 0.3], [GATE_X + 2.6, LOT.z0 + 2]]),
        loop([[GATE_X - 3, 19 + dz], [GATE_X + 3, 19 + dz], [GATE_X + 3, 16 + dz], [GATE_X - 3, 16 + dz]]),
        loop([[COMP.x0 + 2, -18], [COMP.x1 - 2, -18], [COMP.x1 - 2, -17], [COMP.x0 + 2, -17]])];
      // who they are: the crews of bays 1 and 3, the stores team, the packers; outside, visitors and security at the gate
      const insideInfo = [{ kind: "crew", line: real.SMT?.line_id }, { kind: "crew", line: (pick2("SMT", true) || real.FAL)?.line_id },
        { kind: "area", area: "packing", plant: plantId }, { kind: "warehouse", wh: "RM-WH-01", plant: plantId }];
      inside.forEach((c, i) => [0, 0.5].forEach(o => { const w = walker(K, g, M.uniform, true); place(w.p, insideInfo[i]); walkers.push({ ...w, curve: c, u: (o + i * 0.17) % 1, speed: 1.1 + i * 0.1, plant: plantId, inside: true }); }));
      outside.forEach((c, i) => (i ? [0] : [0, 0.17, 0.34, 0.51, 0.68, 0.85]).forEach(o => {     // staff coming in from the car park and going home
        const w = walker(K, g, i === 1 ? M.uniform : [M.visitor, M.uniform, M.accent][Math.round(o * 6) % 3], i === 1); place(w.p, i ? gateInfo : office);
        walkers.push({ ...w, curve: c, u: (i * 0.3 + o) % 1, speed: 1.1 + o * 0.3, plant: plantId, inside: false });
      }));

      // ---- lorries for this plant: in from the public road, through the gate, reverse to a dock door, unload / load, leave ----
      [["out", M.accent, WDOCK.out[1]], ["in", M.cabIn, WDOCK.in[0]]].forEach(([kind, cab, dx], k) => {
        const T = truck(K, cab); scene.add(T.t);
        vehicle(T.t, { id: `${plantId}-LORRY-${kind.toUpperCase()}`, type: "lorry", role: kind, plant: plantId, name: kind === "out" ? "Dispatch lorry" : "Inbound lorry" });
        const side = kind === "out" ? 1 : -1;
        const pts = [local(GOODS_X - 120 * side, ROAD_Z - 1.8 * side), local(GOODS_X, ROAD_Z - 1.8 * side), local(GOODS_X, COMP.z1 - 2), local(dx, COMP.z1 - 2.6), local(dx, WHB.z1 + 3.8)];
        const tl = label(kind === "out" ? "Dispatch lorry" : "Inbound lorry", "tw3-zone tw3-truck"); tl.position.set(0, 3.0, 0); T.t.add(tl);
        trucks.push({ g: T.t, pallets: T.pallets, kind, plant: plantId, pts, ph: (0.15 + k * 0.5 + pi * 0.23) % 1, active: false, label: tl,
          fk: kind === "out" ? fkOut : fkIn, home: kind === "out" ? [48.6, 2.2] : [40.5, 3.6], dock: [dx, WHB.z1 - 0.9] });
      });

      plants[plantId] = { inner, g, P, walls, wallMat, roofMat, skyMat, roof, skylights, edges, roofLabel, racks, staging, packAnim, route, ants, antN, steps, setGate, setGoods, lotCars,
        wh: { wallMat: whWall, roofMat: whRoofMat, roof: whRoof, label: whLabel, inner: whInner, center: local(wcx, wcz), k: null },
        center: local(bcx, bcz), lines: Object.values(lines).filter(l => l.plant === plantId), fal: falL, off: 0, ph: 0, k: null };
    });
    // ---- the neighbourhood: other factories, apartment towers, roadside trees, traffic on the main road and the side road ----
    const nbWall = mat("--surface-hi"), nbRoof = mat("--text-muted"), nbWin = mat("--sky", { emissiveTok: "--sky", emissiveIntensity: 0.3 });
    const factory = (x0, x1, z0, z1, h, name, saw) => {
      const w = x1 - x0, d = z1 - z0, cx = (x0 + x1) / 2, cz = (z0 + z1) / 2;
      box(w + 10, 0.04, d + 10, M.yard, cx, 0.02, cz);
      box(w, h, d, nbWall, cx, h / 2, cz);
      [z0 - 0.03, z1 + 0.03].forEach(wz => box(w * 0.9, 1, 0.06, nbWin, cx, h * 0.62, wz));                 // window bands
      if (saw) {
        const n = Math.max(3, Math.round(d / 6)), tw = d / n, rise = 2;
        for (let i = 0; i < n; i++) { box(w + 0.4, 0.2, Math.hypot(tw, rise), nbRoof, cx, h + rise / 2, z0 + i * tw + tw / 2).rotation.x = -Math.atan2(rise, tw);
          box(w + 0.4, rise, 0.1, nbWin, cx, h + rise / 2, z0 + (i + 1) * tw); }
      } else box(w + 0.4, 0.3, d + 0.4, nbRoof, cx, h + 0.15, cz);
      const across = z0 > ROAD_Z;                                             // across the road: chimney at the back and lower
      stack(x1 - 3, across ? z1 - 3 : z0 + 3, h + (across ? 7 : 14), across ? 0.8 : 1.1);
      const l2 = label(name, "tw3-zone tw3-nb"); l2.position.set(cx, h + 4, cz); scene.add(l2);
    };
    const tower = (cx, cz, w, d, floors) => {
      const h = floors * 3.1;
      box(w, h, d, nbWall, cx, h / 2, cz);
      for (let f = 0; f < floors; f++) box(w + 0.08, 1.2, d + 0.08, nbWin, cx, 1.7 + f * 3.1, cz);           // a glass band on every floor
      box(w + 0.6, 0.5, d + 0.6, nbRoof, cx, h + 0.25, cz);
      cyl(1.2, 1.2, 2.2, frameMat, cx + w / 4, h + 1.5, cz, scene, 12);                                       // water tank
    };
    const R = ROAD_Z;
    factory(-62, -26, R + 13, R + 35, 9, "Auto components factory", true);
    factory(2, 38, R + 15, R + 33, 7, "Packaging factory", false);
    factory(80, 114, -28, 2, 10, "Electronics parts factory", true);
    factory(-24, 16, -66, -38, 8, "Plastics moulding factory", false);
    [[84, R + 24, 14, 12, 13], [102, R + 26, 14, 12, 17], [120, R + 22, 14, 12, 11]].forEach(a => tower(...a));
    const ap1 = label("Apartment towers", "tw3-zone tw3-nb"); ap1.position.set(102, 17 * 3.1 + 4, R + 26); scene.add(ap1);
    [[-80, -26, 16, 14, 18], [-80, -2, 16, 12, 13], [-98, -14, 12, 12, 15]].forEach(a => tower(...a));
    const ap2 = label("Apartments", "tw3-zone tw3-nb"); ap2.position.set(-80, 18 * 3.1 + 4, -26); scene.add(ap2);
    for (let x = -200; x <= 200; x += 12) [R - 7.4, R + 7.4].forEach(tz => {                                  // roadside trees, clear of the gates and roads
      if ((tz < R && (Math.abs(x - GATE_X) < 7 || Math.abs(x - GOODS_X) < 7)) || Math.abs(x - CROSS_X) < 8) return;
      if (tz < R && x > COMP.x0 - 2 && x < COMP.x1 + 2) return;
      cyl(0.15, 0.2, 1.4, M.trunk, x, 0.7, tz, scene, 6);
      const c = new THREE.Mesh(new THREE.ConeGeometry(1.3, 3, 8), M.tree); c.position.set(x, 2.9, tz); scene.add(c);
    });
    // traffic: cars and buses both ways on the main road, cars on the side road (illustrative)
    const traffic = [];
    const tMats = [M.accent, M.cabIn, M.visitor, M.agv, M.surface, darkMat, M.device, M.red];
    for (let i = 0; i < 14; i++) {
      const bus = i % 7 === 3, east = i % 2 === 0, c = car(K, bus ? M.safety : tMats[i % tMats.length], bus ? "bus" : "car"); scene.add(c);
      traffic.push({ c, axis: "x", dir: east ? 1 : -1, s: -320 + ((i * 97) % 640), speed: bus ? 8 : 10 + (i % 4) * 2.4, lane: R + (east ? 1.8 : -1.8) });
    }
    for (let i = 0; i < 5; i++) {
      const c = car(K, tMats[(i * 3 + 2) % tMats.length]); scene.add(c);
      traffic.push({ c, axis: "z", dir: i % 2 ? 1 : -1, s: -180 + i * 45, speed: 8 + i, lane: CROSS_X + (i % 2 ? -1.8 : 1.8) });
    }
    // pedestrians on the pavements
    const walkLine = (x0, x1, z) => new THREE.CatmullRomCurve3([[x0, z - 0.3], [x1, z - 0.3], [x1, z + 0.3], [x0, z + 0.3]].map(([x2, z2]) => new THREE.Vector3(x2, 0, z2)), true, "catmullrom", 0.05);
    [[R - 5, -0.1], [R - 5, 0.45], [R + 5, 0.2], [R + 5, 0.7], [R + 5, 0.9]].forEach(([z, u], i) => {
      const w = walker(K, scene, [M.visitor, M.accent, M.cabIn][i % 3], false);
      walkers.push({ ...w, curve: walkLine(-150, 150, z), u: (u + 1) % 1, speed: 1.2 + i * 0.1, plant: null, inside: false });
    });
    paint();

    // ---- interaction ----
    const ray = new THREE.Raycaster(), mouse = new THREE.Vector2();
    let hover = null, vHover = null;
    const pick = e => {                                              // a machine group, a vehicle root, or a place
      const r = renderer.domElement.getBoundingClientRect();
      mouse.set(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1);
      ray.setFromCamera(mouse, camera);
      const hit = ray.intersectObjects([...machineMeshes, ...vehicles.filter(v => v.visible), ...places.filter(p => p.visible)], true)[0];
      for (let o = hit?.object; o; o = o.parent) { if (o.userData?.group) return o.userData.group; if (o.userData?.vehicle || o.userData?.pick) return o; }
      return null;
    };
    const onMove = e => {
      const found = pick(e), hit = found?.userData.pick ? null : found, v = hit?.userData.vehicle ? hit : null, g = v ? null : hit;
      renderer.domElement.style.cursor = found ? "pointer" : "grab";
      if (v !== vHover) { if (vHover && !vHover.userData.selected) vHover.userData.ring.visible = false; vHover = v; if (v) v.userData.ring.visible = true; }
      if (g === hover) return;
      if (hover && !hover.userData.selected) { hover.userData.tag.visible = false; hover.userData.body.material.emissive.setScalar(0); }
      hover = g;
      if (g) { g.userData.tag.visible = true; g.userData.body.material.emissive.copy(token("--brand-blue")).multiplyScalar(0.25); }
    };
    let down = null;
    const onDown = e => { down = [e.clientX, e.clientY]; };
    const onUp = e => {
      if (!down || Math.hypot(e.clientX - down[0], e.clientY - down[1]) > 4) return;
      const hit = pick(e);
      api.current?.onSelect(!hit ? null : hit.userData.pick ? { ...hit.userData.pick } : hit.userData.vehicle ? { kind: "vehicle", ...hit.userData.vehicle }
        : { kind: "machine", id: hit.userData.machine, line: hit.userData.line, modelled: hit.userData.modelled, type: hit.userData.type });
    };
    renderer.domElement.addEventListener("pointermove", onMove);
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointerup", onUp);

    // ---- animation ----
    const dummy = new THREE.Object3D();
    const clock = new THREE.Clock();
    let focus = null, raf = 0;
    controls.addEventListener("start", () => { focus = null; });
    const focusOn = (target, offset = new THREE.Vector3(7, 9, 12)) => {
      focus = target ? { target: target.clone(), pos: target.clone().add(offset) } : { target: HOME_T.clone(), pos: HOME.clone() };
    };
    const tick = () => {
      raf = requestAnimationFrame(tick);
      if (document.hidden) return;
      const dt = Math.min(0.05, clock.getDelta()), t = clock.elapsedTime, still = reduced();
      Object.values(lines).forEach(l => {
        const pace = PACE[l.state || l.L.state] ?? 0;
        if (!still) { l.offset = (l.offset + dt * 1.6 * pace) % 1.3; l.phase += dt * pace; }
        for (let i = 0; i < l.N; i++) {
          const a = ((i * 1.3 + l.offset) % l.len) - l.len / 2;
          dummy.position.set(l.mid + l.dir * a, 0.65, l.z); dummy.updateMatrix(); l.boards.setMatrixAt(i, dummy.matrix);
          if (l.chips) { dummy.position.y = 0.69; dummy.updateMatrix(); l.chips.setMatrixAt(i, dummy.matrix); }
        }
        l.boards.instanceMatrix.needsUpdate = true;
        if (l.chips) l.chips.instanceMatrix.needsUpdate = true;
        l.anims.forEach(f => f(l.phase, still ? 0 : pace));
        const pulse = st => (still ? 1 : st === "running" ? 1 + 0.18 * Math.sin(t * 4) : st === "down" ? 1 + 0.3 * Math.max(0, Math.sin(t * 7)) : 1);
        l.ms.forEach(m => m.beacon.scale.setScalar(pulse(m.state)));            // each machine's own beacon
        const near = (l.focused || camera.position.distanceTo(l.center) < 26) && plants[l.plant].k < 0.5;
        if (near !== l.near) { l.near = near; l.label.element.classList.toggle("compact", !near); }
      });
      agvs.forEach(a => {
        const pace = PACE[a.line.state || a.line.L.state] ?? 0;
        if (!still) a.t = (a.t + dt * 0.08 * pace) % 1;
        const k = a.t < 0.5 ? a.t * 2 : 2 - a.t * 2, s = k < 0.1 ? 0 : k > 0.9 ? 1 : (k - 0.1) / 0.8;
        const [[x0, z0], [x1, z1]] = a.path;
        a.v.position.set(x0 + (x1 - x0) * s, 0, z0 + (z1 - z0) * s);
        a.v.rotation.y = Math.atan2(-(z1 - z0), x1 - x0) + (a.t < 0.5 ? 0 : Math.PI);
      });
      // per plant: roof/walls fade as you come close, material route, packing, steps
      Object.values(plants).forEach(h => {
        const k = smooth(clamp01((camera.position.distanceTo(h.center) - 48) / 40));        // 1 = far (closed building), 0 = close (open)
        if (h.k === null || Math.abs(k - h.k) > 0.005) {
          h.k = k;
          h.roofMat.opacity = 0.04 + 0.96 * k; h.skyMat.opacity = 0.04 + 0.96 * k; h.wallMat.opacity = 0.2 + 0.8 * k;
          h.roofMat.depthWrite = h.skyMat.depthWrite = h.wallMat.depthWrite = k > 0.95;
          h.roof.visible = k > 0.02; h.skylights.forEach(s => { s.visible = k > 0.02; });
          h.roofLabel.visible = k > 0.5;
          h.steps.forEach(s => s.classList.toggle("compact", k > 0.25));
          h.steps.forEach(s => { s.style.visibility = k > 0.85 ? "hidden" : ""; });
          h.inner.forEach(o => { o.visible = k < 0.6; });
          h.lines.forEach(l => { l.label.visible = k < 0.7; });
        }
        const w2 = h.wh, kw = smooth(clamp01((camera.position.distanceTo(w2.center) - 40) / 40));        // the warehouse opens up too
        if (w2.k === null || Math.abs(kw - w2.k) > 0.005) {
          w2.k = kw;
          w2.roofMat.opacity = 0.04 + 0.96 * kw; w2.wallMat.opacity = 0.2 + 0.8 * kw; w2.roofMat.depthWrite = w2.wallMat.depthWrite = kw > 0.95;
          w2.roof.visible = kw > 0.02; w2.label.visible = kw > 0.5;
          h.racks.forEach(r => { r.label.visible = kw < 0.6; }); w2.inner.forEach(o => { o.visible = kw < 0.6; });
        }
        const paces = h.lines.filter(l => l.record).map(l => PACE[l.state || l.L.state] ?? 0);
        const pace = paces.length ? paces.reduce((a, b) => a + b, 0) / paces.length : 0, len = h.route.getLength();
        if (!still) { h.off = (h.off + dt * 1.2 * pace) % len; h.ph += dt * (PACE[h.fal?.state || h.fal?.L.state] ?? 0); }
        for (let i = 0; i < h.antN; i++) {
          const u = ((i * 1.1 + h.off) % len) / len;
          dummy.position.copy(h.route.getPointAt(u)); dummy.quaternion.setFromUnitVectors(UP, h.route.getTangentAt(u));
          dummy.updateMatrix(); h.ants.setMatrixAt(i, dummy.matrix);
        }
        dummy.quaternion.identity();
        h.ants.instanceMatrix.needsUpdate = true;
        h.packAnim(h.ph, still ? 0 : PACE[h.fal?.state || h.fal?.L.state] ?? 0);
      });
      // people walking
      walkers.forEach(w => {
        if (!still) w.u = (w.u + dt * w.speed / w.curve.getLength()) % 1;
        const p = w.curve.getPointAt(w.u), tg = w.curve.getTangentAt(w.u);
        w.p.position.set(p.x, 0, p.z); w.p.rotation.y = Math.atan2(-tg.z, tg.x);
        w.step(still ? 0 : w.u * w.curve.getLength() * 2.6);
      });
      // lorries
      const gates = {};
      trucks.forEach(tr => {
        tr.g.visible = tr.active;
        const fk = tr.fk;
        if (!tr.active) { fk.f.position.set(tr.home[0], 0, tr.home[1]); fk.load.visible = false; return; }
        if (!still) tr.ph = (tr.ph + dt / 48) % 1;
        const tt = tr.ph, [E, R0, G, Y, D] = tr.pts;
        let pos, face;
        if (tt < 0.24) { const a = along([E, R0, G, Y], smooth(tt / 0.24)); pos = a.p; face = a.d; }
        else if (tt < 0.32) { pos = Y.clone().lerp(D, smooth((tt - 0.24) / 0.08)); face = Y.clone().sub(D); }
        else if (tt < 0.62) { pos = D.clone(); face = Y.clone().sub(D); }
        else if (tt < 0.7) { pos = D.clone().lerp(Y, smooth((tt - 0.62) / 0.08)); face = Y.clone().sub(D); }
        else { const a = along([Y, G, R0, E], smooth((tt - 0.7) / 0.3)); pos = a.p; face = a.d; }
        tr.g.position.copy(pos);
        const want = Math.atan2(-face.z, face.x);
        let dA = want - tr.g.rotation.y; dA = Math.atan2(Math.sin(dA), Math.cos(dA));
        tr.g.rotation.y += dA * Math.min(1, dt * 6);
        if (pos.distanceTo(G) < 9) gates[tr.plant] = true;
        const prog = tt < 0.32 ? 0 : tt < 0.62 ? (tt - 0.32) / 0.3 : 1;
        const shown = tr.kind === "out" ? Math.floor(6 * prog) : 6 - Math.floor(6 * prog);
        tr.pallets.forEach((p, i) => { p.visible = i < shown; });
        if (tt >= 0.32 && tt < 0.62) {
          const kk = (prog * 3) % 1, go = kk < 0.5, s = smooth(go ? kk * 2 : 2 - kk * 2);
          const [a, b] = tr.kind === "out" ? [tr.home, tr.dock] : [tr.dock, tr.home];
          fk.f.position.set(a[0] + (b[0] - a[0]) * s, 0, a[1] + (b[1] - a[1]) * s);
          fk.f.rotation.y = Math.atan2(-(b[1] - a[1]) * (go ? 1 : -1), (b[0] - a[0]) * (go ? 1 : -1)) + (go ? 0 : Math.PI);
          fk.load.visible = go;
        } else { fk.f.position.set(tr.home[0], 0, tr.home[1]); fk.load.visible = false; }
      });
      const staff = {};
      Object.entries(plants).forEach(([id, h]) => h.lotCars.forEach(c => {         // staff cars: in through the staff gate, park, later drive home
        if (!still) c.ph = (c.ph + dt / c.dur) % 1;
        const tt = c.ph, [E, R0, G, A, B, R1, E1] = c.pts;
        let pos, face;
        if (tt < 0.22) { const a = along([E, R0, G, A], smooth(tt / 0.22)); pos = a.p; face = a.d; }
        else if (tt < 0.28) { pos = A.clone().lerp(B, smooth((tt - 0.22) / 0.06)); face = B.clone().sub(A); }
        else if (tt < 0.7) { pos = B.clone(); face = B.clone().sub(A); }
        else if (tt < 0.76) { pos = B.clone().lerp(A, smooth((tt - 0.7) / 0.06)); face = B.clone().sub(A); }
        else { const a = along([A, G, R1, E1], smooth((tt - 0.76) / 0.24)); pos = a.p; face = a.d; }
        c.c.position.copy(pos);
        let dA = Math.atan2(-face.z, face.x) - c.c.rotation.y; dA = Math.atan2(Math.sin(dA), Math.cos(dA));
        c.c.rotation.y += dA * Math.min(1, dt * 6);
        if (pos.distanceTo(G) < 7) staff[id] = true;
      }));
      Object.entries(plants).forEach(([id, h]) => { h.setGoods(!!gates[id]); h.setGate(!!staff[id]); });
      traffic.forEach(v => {
        if (!still) v.s += v.dir * v.speed * dt;
        if (v.s > 330) v.s -= 660; if (v.s < -330) v.s += 660;
        if (v.axis === "x") { v.c.position.set(v.s, 0, v.lane); v.c.rotation.y = v.dir > 0 ? 0 : Math.PI; }
        else { const z = ROAD_Z - 8 - ((v.s + 330) % 660) * 0.3; v.c.position.set(v.lane, 0, z); v.c.rotation.y = v.dir > 0 ? Math.PI / 2 : -Math.PI / 2; }
      });
      smoke.forEach(p => {                                                     // puffs rise, drift with the wind, grow and fade
        if (!still) p.t = (p.t + dt * 0.11) % 1;
        p.puff.position.set(p.x + p.t * 4, p.y + p.t * 10, p.z - p.t * 1.5);
        p.puff.scale.setScalar(0.5 + p.t * 2.4);
        p.m.opacity = 0.42 * (1 - p.t);
      });
      if (focus) {
        const k = still ? 1 : 1 - Math.exp(-dt * 3.5);
        camera.position.lerp(focus.pos, k); controls.target.lerp(focus.target, k);
        if (camera.position.distanceTo(focus.pos) < 0.05) focus = null;
      }
      controls.update();
      renderer.render(scene, camera);
      labels.render(scene, camera);
    };
    tick();

    const ro = new ResizeObserver(() => { camera.aspect = W() / H(); camera.updateProjectionMatrix(); renderer.setSize(W(), H()); labels.setSize(W(), H()); });
    ro.observe(el);
    const mo = new MutationObserver(paint);
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });

    api.current = { plants, lines, trucks, machineMeshes, vehicles, token, onSelect, focusOn, home: () => { camera.position.copy(HOME); controls.target.copy(HOME_T); },
      warehouse: () => { const h = Object.values(plants)[0]; if (h) focusOn(h.wh.center, new THREE.Vector3(4, 22, 26)); },
      fitHome: () => focusOn(null) };
    return () => {
      cancelAnimationFrame(raf); ro.disconnect(); mo.disconnect(); controls.dispose();
      renderer.domElement.removeEventListener("pointermove", onMove);
      renderer.domElement.removeEventListener("pointerdown", onDown);
      renderer.domElement.removeEventListener("pointerup", onUp);
      scene.traverse(o => { o.geometry?.dispose(); if (o.material) [].concat(o.material).forEach(m => m.dispose()); });
      renderer.dispose();
      renderer.domElement.remove(); labels.domElement.remove();      // the toolbar inside the host is React's: leave it
      api.current = null;
    };
  }, [plant]);                                                                                   // eslint-disable-line react-hooks/exhaustive-deps

  // every hour shown: line states and boards, operators, rejects, rack fill, pallet staging, lorries
  useEffect(() => {
    const A = api.current;
    if (!A || !snap) return;
    A.onSelect = onSelect;
    const fmt = v => Intl.NumberFormat("en").format(v), cf = v => Intl.NumberFormat("en", { notation: "compact" }).format(v);
    Object.values(A.lines).forEach(l => {
      const L = l.record ? snap.lines.find(x => x.line_id === l.L.line_id) || l.L : l.L;
      l.L = L; l.state = L.state;
      l.ms.forEach(e => {                                             // each machine in its own state this hour
        const st = (L.machines || []).find(x => x.machine_id === e.id)?.state || L.state;
        const tk = A.token(STATE_TOKEN[st] || STATE_TOKEN.no_data);
        e.state = st; e.band.color.copy(tk); e.band.emissive.copy(tk); e.beaconMat.color.copy(tk); e.beaconMat.emissive.copy(tk);
      });
      l.setAndon(L.state);
      const h = L.hour;
      l.crew.forEach(c => { c.visible = !!h; });
      const fill = h ? Math.min(1, h.reject / 20) : 0;
      l.rejectFill.scale.y = Math.max(0.02, fill * 0.5); l.rejectFill.position.y = 0.08 + l.rejectFill.scale.y / 2;
      const el = l.label.element;
      const row = (k, v, cls = "") => { const r = document.createElement("div"); r.className = "tw3-row " + cls; const a = document.createElement("span"); a.textContent = k;
        const b = document.createElement("b"); b.textContent = v; r.append(a, b); return r; };
      const head = document.createElement("div"); head.className = "tw3-board-h";
      head.textContent = l.record ? `Bay ${l.bay} · ${L.line_id}${L.generated ? " (generated)" : ""} · ${STATE_LABEL[L.state]}` : `Bay ${l.bay} · no production records`;
      const sum = document.createElement("span"); sum.className = "tw3-sum"; sum.textContent = h ? ` · ${fmt(h.good)}/${fmt(h.target)}` : "";
      head.append(sum);
      el.replaceChildren(head, ...(!l.record ? [row("Line", `${L.kind === "SMT" ? "SMT" : "Final assembly"} · modelled`), row("Data", "none recorded")]
        : h ? [row("Product", h.product_id), row("Order", h.production_order_id), row("Shift · team", `${h.shift} · ${h.team}`),
          row("Good / target", `${fmt(h.good)} / ${fmt(h.target)}`), row("OEE", L.hour_oee?.oee == null ? "–" : `${L.hour_oee.oee}%`), row("Rejects", fmt(h.reject)),
          ...(h.downtime_min ? [row("Stopped", `${h.downtime_min} min · ${h.downtime_reason ? h.downtime_reason.toLowerCase().replace(/_/g, " ") : "reason not recorded"}`, "stop")] : [])]
          : [row("This hour", "no record")]));
      el.dataset.state = L.state;
    });
    const all = snap.stock?.warehouses || [];
    const max = Math.max(1, ...all.map(w => w.on_hand));
    const flow = Object.fromEntries((snap.flow || []).map(f => [f.plant_id, f]));
    const maxFg = Math.max(1, ...(snap.flow || []).map(f => f.fg));
    Object.entries(A.plants).forEach(([pid, h]) => {
      h.racks.forEach(r => {
        const w = all.find(x => x.plant_id === pid && x.warehouse_id === r.wh);
        const n = Math.round(((w ? w.on_hand : 0) / max) * r.crates.length);
        r.crates.forEach((c, i) => { c.visible = i < n; });
        r.label.element.textContent = `${r.title}${w ? ` · ${cf(w.on_hand)}` : ""}`;
      });
      const n = flow[pid] ? Math.round((9 * flow[pid].fg) / maxFg) : 0;
      h.staging.forEach((s, i) => { s.visible = i < n; });
      const P = snap.plants.find(p => p.plant_id === pid);
      h.roofLabel.element.textContent = `Plant ${pid}${P?.oee != null ? ` · OEE ${P.oee}%` : ""}`;
    });
    const dsp = snap.dispatch || { orders: 0 };
    A.trucks.forEach(tr => {
      const f = flow[tr.plant];
      tr.active = tr.kind === "out" ? dsp.orders > 0 : (f?.inbound || 0) > 0;
      tr.label.element.textContent = tr.kind === "out" ? `Dispatch lorry · ${dsp.orders} orders due today` : `Inbound lorry · ${cf(f?.inbound || 0)} in transit`;
    });
  }, [snap, onSelect, plant]);

  // selection: highlight the machine or vehicle and bring it close (the roof opens as the camera comes in)
  useEffect(() => {
    const A = api.current;
    if (!A) return;
    A.machineMeshes.forEach(m => {
      const g = m.userData.group, on = sel?.kind === "machine" && g.userData.machine === sel.id;
      g.userData.selected = on;
      g.userData.tag.visible = on;
      m.material.emissive.copy(on ? A.token("--brand-blue") : new THREE.Color(0)).multiplyScalar(on ? 0.35 : 0);
    });
    let v = null;
    A.vehicles.forEach(x => { const on = sel?.kind === "vehicle" && x.userData.vehicle.id === sel.id; x.userData.selected = on; x.userData.ring.visible = on; if (on) v = x; });
    const lineId = sel?.kind === "line" ? sel.id : sel?.kind === "machine" ? A.machineMeshes.find(m => m.userData.group.userData.machine === sel.id)?.userData.group.userData.line : null;
    Object.entries(A.lines).forEach(([id, l]) => { l.focused = id === lineId; });
    if (lineId && A.lines[lineId]) A.focusOn(A.lines[lineId].center);
    else if (v) A.focusOn(v.getWorldPosition(new THREE.Vector3()), new THREE.Vector3(6, 10, 14));
  }, [sel, plant]);

  const inside = () => { const A = api.current, h = A && Object.values(A.plants)[0]; if (h) A.focusOn(h.center, new THREE.Vector3(6, 26, 30)); };
  return (
    <div className="tw3" ref={host} role="img"
      aria-label={`3D view of plant ${plant} at ${snap?.at || ""}: ${(snap?.lines || []).filter(l => l.plant_id === plant).map(l => `${l.line_id} ${l.state}`).join(", ")}. The line cards view lists the same information.`}>
      <div className="tw3-tools">
        <div className="tw3-btns">
          <button className="sp-btn" onClick={() => api.current?.fitHome()}><Icon3D /> Whole site</button>
          <button className="sp-btn" onClick={inside}>Inside the production hall</button>
          <button className="sp-btn" onClick={() => api.current?.warehouse()}>Inside the warehouse</button>
        </div>
        <div className="tw3-legend" aria-hidden="true">
          <span><i className="t-ok" />Running</span><span><i className="t-warn" />Slowed</span><span><i className="t-bad" />Down</span><span><i className="t-none" />No records</span>
        </div>
      </div>
      <small className="tw3-hint">Drag to rotate · scroll to zoom in (the roof opens) · right-drag to pan · click any machine, rack, forklift, AGV or lorry · people, traffic and neighbouring buildings are illustrative</small>
    </div>
  );
}

const Icon3D = () => (
  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9z" /><path d="M12 12l8-4.5M12 12v9M12 12L4 7.5" />
  </svg>
);
