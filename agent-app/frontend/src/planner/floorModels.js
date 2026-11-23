import * as THREE from "three";

// Detailed models of the line equipment for the 3D twin. Each builder adds parts to the machine group `g` (origin at floor centre of
// the machine, x along the line) and returns animators: functions (phase, pace) -> void. `phase` only advances while the line runs
// (it is the line's own clock), so every moving part stops when the line is down and slows when it is slowed.
// K = { box(w,h,d,mat,x,y,z,parent), cyl(rt,rb,h,mat,x,y,z,parent,seg), M: shared materials }

function gantry(K, g, w, h, d, headMat, speed = 1.6) {
  const { box, M } = K;
  box(0.08, 0.3, d * 0.86, M.dark, -w * 0.38, h + 0.1, 0, g);
  box(0.08, 0.3, d * 0.86, M.dark, w * 0.38, h + 0.1, 0, g);
  const bridge = box(w * 0.8, 0.1, 0.14, M.frame, 0, h + 0.28, 0, g);
  const head = box(0.26, 0.3, 0.26, headMat, 0, h + 0.12, 0, g);
  return (ph) => {
    const z = Math.sin(ph * speed) * d * 0.32, x = Math.sin(ph * speed * 1.7) * w * 0.3;
    bridge.position.z = z; head.position.set(x, h + 0.12 - Math.max(0, Math.sin(ph * speed * 3.4)) * 0.08, z);
  };
}

const BUILD = {
  SPI(K, g, w, h, d) {
    const { box, M } = K;
    box(w * 0.7, h * 0.35, 0.03, M.glass, 0, h * 0.62, d / 2 + 0.01, g);              // front window
    box(w * 0.3, 0.18, 0.02, M.screen, -w * 0.25, h * 0.88, d / 2 + 0.02, g);          // HMI screen
    return [gantry(K, g, w, h, d, M.accent, 1.2)];
  },
  "Pick and Place"(K, g, w, h, d) {
    const { box, cyl, M } = K;
    box(w * 0.9, 0.5, d * 0.8, M.glass, 0, h + 0.25, 0, g);                            // glass hood
    for (let i = 0; i < 9; i++) {                                                       // feeder bank with reels, both sides
      const x = -w * 0.4 + (i * w * 0.8) / 8;
      [-1, 1].forEach(s => {
        box(0.16, 0.5, 0.28, M.dark, x, h * 0.55, s * (d / 2 + 0.16), g);
        const reel = cyl(0.17, 0.17, 0.05, i % 3 ? M.accent : M.heat, x, h * 0.62, s * (d / 2 + 0.34), g, 16);
        reel.rotation.x = Math.PI / 2;
      });
    }
    box(w * 0.3, 0.2, 0.02, M.screen, w * 0.25, h * 0.8, d / 2 + 0.5, g);
    const head = gantry(K, g, w, h + 0.02, d, M.heat, 3.2);                            // fast head inside the hood
    return [head];
  },
  "Reflow Oven"(K, g, w, h, d) {
    const { box, cyl, M } = K;
    const zones = 7, zw = (w * 0.86) / zones, glows = [];
    for (let i = 0; i < zones; i++) {                                                  // heating zones, hottest in the middle
      const hot = 1 - Math.abs(i - (zones - 1) / 2) / zones;
      const m = M.heat.clone(); m.emissiveIntensity = 0.25 + hot * 0.9; K.track(m, "--brand-orange", "--brand-orange");
      glows.push(box(zw * 0.86, 0.06, d * 0.7, m, -w * 0.43 + zw * (i + 0.5), h + 0.13, 0, g));
      box(0.03, h * 0.5, 0.02, M.dark, -w * 0.43 + zw * i, h * 0.55, d / 2 + 0.01, g);
    }
    [-w * 0.3, w * 0.3].forEach(x => cyl(0.14, 0.14, 0.7, M.frame, x, h + 0.45, -d * 0.25, g, 12));   // exhaust stacks
    box(0.5, 0.18, 0.02, M.screen, w * 0.35, h * 0.8, d / 2 + 0.02, g);
    return [(ph, pace) => glows.forEach((z, i) => { z.material.emissiveIntensity = (0.2 + 0.8 * (1 - Math.abs(i - 3) / 7)) * (0.35 + 0.65 * pace); })];
  },
  AOI(K, g, w, h, d) {
    const { box, cyl, M } = K;
    box(w * 0.7, h * 0.3, 0.03, M.glass, 0, h * 0.65, d / 2 + 0.01, g);
    const ring = cyl(0.22, 0.22, 0.05, M.led, 0, h * 0.62, 0, g, 24);
    box(0.3, 0.18, 0.02, M.screen, -w * 0.25, h * 0.88, d / 2 + 0.02, g);
    const cam = gantry(K, g, w, h, d, M.accent, 0.9);
    return [cam, (ph, pace) => { ring.material.emissiveIntensity = pace ? 0.6 + 0.4 * Math.sin(ph * 6) : 0.1; }];
  },
  "ICT Tester"(K, g, w, h, d) {
    const { box, M } = K;
    box(w * 0.8, 0.12, d * 0.7, M.dark, 0, h * 0.62, 0, g);                            // fixture bed
    [-1, 1].forEach(s => box(0.08, h * 0.6, 0.08, M.frame, s * w * 0.35, h + 0.25, 0, g));
    const press = box(w * 0.75, 0.16, d * 0.65, M.accent, 0, h + 0.4, 0, g);
    box(0.3, 0.18, 0.02, M.screen, w * 0.25, h * 0.88, d / 2 + 0.02, g);
    return [(ph) => { press.position.y = h + 0.2 + Math.max(0, Math.sin(ph * 1.6)) * 0.35; }];
  },
  "Assembly Cell"(K, g, w, h, d) {
    const { box, cyl, M } = K;
    box(w * 0.6, 0.12, d * 0.5, M.dark, 0, 0.95, d * 0.1, g);                           // work table
    cyl(0.25, 0.3, 0.3, M.dark, -w * 0.15, 1.15, -d * 0.2, g, 16);                     // robot base
    const arm = new THREE.Group(); arm.position.set(-w * 0.15, 1.3, -d * 0.2); g.add(arm);
    const up = box(0.16, 0.8, 0.16, M.heat, 0, 0.4, 0, arm);
    const fore = new THREE.Group(); fore.position.set(0, 0.8, 0); arm.add(fore);
    box(0.7, 0.13, 0.13, M.heat, 0.35, 0, 0, fore);
    box(0.12, 0.2, 0.12, M.dark, 0.72, -0.12, 0, fore);                                 // screwdriver tool
    [-1, 1].forEach(s => box(0.06, 1.8, 0.06, M.led, s * (w / 2 + 0.2), 1.0, d / 2 + 0.2, g));   // light-curtain posts
    up.castShadow = false;
    return [(ph) => { arm.rotation.y = Math.sin(ph * 1.3) * 1.1; fore.rotation.z = -0.4 + Math.sin(ph * 2.6) * 0.35; }];
  },
  "RF Tester"(K, g, w, h, d) {
    const { box, cyl, M } = K;
    box(w * 0.8, h * 0.5, 0.03, M.dark, 0, h * 0.5, d / 2 + 0.01, g);                  // shielded door
    cyl(0.03, 0.03, 0.8, M.frame, w * 0.3, h + 0.5, 0, g, 8);                          // antenna
    const led = box(0.12, 0.12, 0.02, M.led, -w * 0.3, h * 0.85, d / 2 + 0.02, g);
    return [(ph, pace) => { led.material.emissiveIntensity = pace && Math.sin(ph * 5) > 0 ? 1.2 : 0.1; }];
  },
  "Camera Tester"(K, g, w, h, d) {
    const { box, cyl, M } = K;
    box(w * 0.7, h * 0.55, 0.04, M.screen, 0, h + 0.4, -d * 0.35, g);                  // calibration chart
    cyl(0.04, 0.04, 0.5, M.frame, 0, h + 0.25, d * 0.15, g, 8);
    const rig = box(0.3, 0.2, 0.2, M.accent, 0, h + 0.55, d * 0.15, g);
    return [(ph) => { rig.rotation.y = Math.sin(ph * 1.1) * 0.5; }];
  },
  "Aging Chamber"(K, g, w, h, d) {
    const { box, M } = K;
    const leds = [];
    for (let r = 0; r < 8; r++) for (let c = 0; c < 12; c++) {                          // 96 channels
      const m = M.led.clone(); K.track(m, "--chart-c4", "--chart-c4");
      leds.push(box(0.08, 0.08, 0.02, m, -w * 0.4 + (c * w * 0.8) / 11, 0.35 + (r * (h - 0.5)) / 7, d / 2 + 0.01, g));
    }
    return [(ph, pace) => leds.forEach((l, i) => { l.material.emissiveIntensity = pace ? (Math.sin(ph * 2 + i * 1.7) > -0.2 ? 1 : 0.15) : 0.08; })];
  },
  "Leak Tester"(K, g, w, h, d) {
    const { box, cyl, M } = K;
    const dial = cyl(0.2, 0.2, 0.05, M.screen, 0, h * 0.7, d / 2 + 0.03, g, 20); dial.rotation.x = Math.PI / 2;
    const needle = box(0.02, 0.17, 0.01, M.red, 0, h * 0.7 + 0.07, d / 2 + 0.07, g);
    box(w * 0.6, 0.3, d * 0.5, M.glass, 0, h + 0.25, 0, g);                             // test chamber
    return [(ph, pace) => { needle.rotation.z = -1 + 2 * (0.5 + 0.5 * Math.sin(ph * 1.4)) * (pace ? 1 : 0); }];
  },
};

export function buildMachine(K, type, g, w, h, d) {
  return (BUILD[type] || (() => []))(K, g, w, h, d);
}

// a person: body, head and safety helmet (illustrative: the data records teams, not headcounts)
export function operator(K, parent, x, z, facing = 0) {
  const { cyl, M } = K;
  const p = new THREE.Group(); p.position.set(x, 0, z); p.rotation.y = facing; parent.add(p);
  cyl(0.22, 0.26, 1.1, M.uniform, 0, 0.75, 0, p, 12);
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.17, 14, 10), M.skin); head.position.y = 1.45; p.add(head);
  const helm = new THREE.Mesh(new THREE.SphereGeometry(0.19, 14, 8, 0, Math.PI * 2, 0, Math.PI / 2), M.heat); helm.position.y = 1.5; p.add(helm);
  return p;
}

// andon tower: red / amber / green; the lit lamp follows the line state
export function andon(K, parent, x, z) {
  const { cyl, M } = K;
  cyl(0.04, 0.04, 2.2, M.dark, x, 1.1, z, parent, 8);
  const lamps = {};
  [["down", "--depth-0", 2.55], ["slowed", "--brand-orange", 2.3], ["running", "--chart-c4", 2.05]].forEach(([k, tok, y]) => {
    const m = new THREE.MeshStandardMaterial({ roughness: 0.4, transparent: true });
    K.track(m, tok, tok);
    lamps[k] = cyl(0.13, 0.13, 0.22, m, x, y, z, parent, 16);
  });
  return state => Object.entries(lamps).forEach(([k, l]) => { const on = k === state; l.material.emissiveIntensity = on ? 1.1 : 0.03; l.material.opacity = on ? 1 : 0.35; });
}

// ---- logistics: trucks, forklifts, the plant gate, the packing station ----

// flatbed truck; `pallets` are shown as it is loaded (outbound) or hidden as it is unloaded (inbound)
export function truck(K, cabMat) {
  const { box, cyl, M } = K;
  const t = new THREE.Group();
  box(1.6, 1.5, 1.9, cabMat, 2.6, 1.15, 0, t);                         // cab (front = +x)
  box(0.05, 0.6, 1.6, M.glass, 3.42, 1.45, 0, t);                     // windscreen
  box(4.6, 0.25, 2.0, M.dark, -0.6, 0.6, 0, t);                       // flatbed
  box(0.12, 1.4, 2.0, M.dark, -2.85, 1.3, 0, t);                      // headboard at the back
  [[2.6, 0.95], [-1.8, 0.95], [-0.6, 0.95]].forEach(([x]) => [-1, 1].forEach(s => {
    const w = cyl(0.38, 0.38, 0.3, M.dark, x, 0.38, s * 0.95, t, 14); w.rotation.x = Math.PI / 2;
  }));
  const pallets = [];
  for (let i = 0; i < 6; i++) {
    const pg = new THREE.Group(); pg.position.set(1.4 - (i % 3) * 1.35, 0.75, (i < 3 ? -1 : 1) * 0.48); t.add(pg);
    box(1.1, 0.14, 0.86, M.wood, 0, 0.07, 0, pg);
    box(1.0, 0.7, 0.8, M.carton, 0, 0.5, 0, pg);
    pallets.push(pg);
  }
  return { t, pallets };
}

export function forklift(K) {
  const { box, cyl, M } = K;
  const f = new THREE.Group();
  box(1.3, 0.7, 0.9, M.heat, 0, 0.55, 0, f);                          // body
  box(0.7, 0.08, 0.8, M.dark, -0.1, 1.55, 0, f);                      // roof
  [[-0.4, -0.35], [-0.4, 0.35], [0.25, -0.35], [0.25, 0.35]].forEach(([x, z]) => box(0.06, 0.9, 0.06, M.dark, x, 1.1, z, f));
  box(0.08, 1.9, 0.7, M.dark, 0.72, 1.0, 0, f);                       // mast
  [-0.22, 0.22].forEach(z => box(0.8, 0.06, 0.12, M.dark, 1.1, 0.2, z, f));   // forks
  const load = new THREE.Group(); load.position.set(1.1, 0.3, 0); f.add(load);
  box(0.9, 0.12, 0.8, M.wood, 0, 0, 0, load); box(0.8, 0.55, 0.7, M.carton, 0, 0.35, 0, load);
  [[-0.45, 0.45], [0.45, 0.45], [-0.45, -0.45], [0.45, -0.45]].forEach(([x, z]) => {
    const w = cyl(0.2, 0.2, 0.16, M.dark, x, 0.2, z, f, 12); w.rotation.x = Math.PI / 2;
  });
  return { f, load };
}

// gate across the road: two posts, a guard hut and a barrier arm that lifts for trucks
export function gate(K, parent, x, z, roadW) {
  const { box, M } = K;
  box(0.3, 1.4, 0.3, M.dark, x - roadW / 2 - 0.4, 0.7, z, parent);
  box(1.6, 2.2, 1.6, M.frame, x + roadW / 2 + 1.4, 1.1, z, parent);   // guard hut
  const pivot = new THREE.Group(); pivot.position.set(x - roadW / 2 - 0.4, 1.25, z); parent.add(pivot);
  box(roadW + 0.6, 0.12, 0.12, M.red, (roadW + 0.6) / 2, 0, 0, pivot);
  return open => { pivot.rotation.z += ((open ? 1.3 : 0) - pivot.rotation.z) * 0.12; };
}

// packing station: table, a packer, finished phones going into cartons
export function packing(K, parent, x, z) {
  const { box, M } = K;
  const g = new THREE.Group(); g.position.set(x, 0, z); parent.add(g);
  box(2.4, 0.08, 1.0, M.frame, 0, 0.95, 0, g);
  [[-1.1, -0.4], [1.1, -0.4], [-1.1, 0.4], [1.1, 0.4]].forEach(([px, pz]) => box(0.06, 0.95, 0.06, M.dark, px, 0.47, pz, g));
  const cartons = [0, 1, 2].map(i => box(0.5, 0.4, 0.4, M.carton, -0.7 + i * 0.6, 1.2, 0, g));
  const phone = box(0.18, 0.04, 0.3, M.device, 0.9, 1.02, 0, g);
  operator(K, g, 0, 0.9, Math.PI);
  return (ph, pace) => { phone.position.x = 0.9 - ((ph * 0.6) % 1) * 1.6 * (pace ? 1 : 0); cartons.forEach((c, i) => { c.position.y = 1.2 + (pace ? Math.max(0, Math.sin(ph * 2 + i)) * 0.06 : 0); }); };
}

// a walking person (illustrative): legs and arms swing with the walk; staff wear hard hats
export function walker(K, parent, bodyMat, hardHat = true) {
  const { box, M } = K;
  const p = new THREE.Group(); parent.add(p);
  const limb = (x, y, h, mat) => { const piv = new THREE.Group(); piv.position.set(0, y, x); p.add(piv); box(0.16, h, 0.16, mat, 0, -h / 2, 0, piv); return piv; };
  const legL = limb(-0.12, 0.85, 0.85, M.dark), legR = limb(0.12, 0.85, 0.85, M.dark);
  box(0.3, 0.62, 0.46, bodyMat, 0, 1.17, 0, p);                                   // torso
  const armL = limb(-0.3, 1.45, 0.62, bodyMat), armR = limb(0.3, 1.45, 0.62, bodyMat);
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.15, 12, 10), M.skin); head.position.y = 1.66; p.add(head);
  if (hardHat) { const hat = new THREE.Mesh(new THREE.SphereGeometry(0.17, 12, 8, 0, Math.PI * 2, 0, Math.PI / 2), M.heat); hat.position.y = 1.7; p.add(hat); }
  return { p, step: ph => { const s = Math.sin(ph) * 0.55; legL.rotation.z = s; legR.rotation.z = -s; armL.rotation.z = -s * 0.8; armR.rotation.z = s * 0.8; } };
}

// a car or a bus (illustrative traffic): length along x, nose towards +x
export function car(K, bodyMat, kind = "car") {
  const { box, cyl, M } = K;
  const c = new THREE.Group();
  const bus = kind === "bus";
  if (bus) {
    box(9, 2.5, 2.4, bodyMat, 0, 1.6, 0, c);                                         // body
    box(8.4, 0.8, 2.44, M.glass, -0.2, 2.2, 0, c);                                   // window band
    box(0.06, 1.4, 2.0, M.glass, 4.52, 1.9, 0, c);                                   // windscreen
  } else {
    box(3.9, 0.7, 1.7, bodyMat, 0, 0.6, 0, c);                                       // body
    box(2.0, 0.6, 1.5, M.glass, -0.2, 1.25, 0, c);                                   // cabin glass
    box(2.0, 0.08, 1.52, bodyMat, -0.2, 1.58, 0, c);                                 // roof
  }
  (bus ? [3, -3] : [1.25, -1.25]).forEach(x => [-1, 1].forEach(s => {
    const w = cyl(bus ? 0.5 : 0.33, bus ? 0.5 : 0.33, 0.26, M.dark, x, bus ? 0.5 : 0.33, s * (bus ? 1.1 : 0.82), c, 12); w.rotation.x = Math.PI / 2;
  }));
  return c;
}
