import { useEffect, useRef } from "react";
import L from "leaflet";
import "leaflet/dist/leaflet.css";

// The plants on a satellite map. Each plant is a card at its site showing how it runs this hour; clicking it opens the plant.
// Imagery and place names are public map tiles; plant positions come from the API (city level unless real sites are configured).

const IMAGERY = "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}";
const PLACES = "https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}";
const STATE = { running: "Running", slowed: "Slowed", down: "Down", no_data: "No data" };
const TONE = { running: "ok", slowed: "warn", down: "bad", no_data: "none" };

function card(s) {
  const el = document.createElement("button");
  el.type = "button";
  el.className = "pm-card t-" + (s.down ? "bad" : s.running < s.lines ? "warn" : "ok");
  el.setAttribute("aria-label", `Open plant ${s.plant_id} in ${s.city}`);
  const add = (tag, cls, text, parent = el) => { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = text; parent.append(x); return x; };
  const head = add("span", "pm-head");
  add("i", "pm-dot", null, head);
  add("b", null, s.city || s.plant_id, head);
  add("small", null, s.plant_id, head);
  const lines = add("span", "pm-lines");
  Object.entries(s.states).forEach(([id, st]) => { const x = add("span", "pm-line t-" + TONE[st], null, lines); add("i", null, null, x); add("span", null, `${id} · ${STATE[st]}`, x); });
  const k = add("span", "pm-kpis");
  const kv = (label, v) => { const x = add("span", null, null, k); add("small", null, label, x); add("b", null, v, x); };
  kv("OEE this hour", s.oee == null ? "–" : `${s.oee}%`);
  kv("Good / target", `${Intl.NumberFormat("en").format(s.hour.good)} / ${Intl.NumberFormat("en").format(s.hour.target)}`);
  if (s.open_problems || s.open_alerts) add("span", "pm-flags", [s.open_problems && `${s.open_problems} open problem${s.open_problems > 1 ? "s" : ""}`, s.open_alerts && `${s.open_alerts} alert${s.open_alerts > 1 ? "s" : ""}`].filter(Boolean).join(" · "));
  add("span", "pm-open", "Open the plant ›");
  return el;
}

export default function PlantMap({ sites, onOpen }) {
  const host = useRef();
  const map = useRef(null);
  const layer = useRef(null);
  const open = useRef(onOpen);
  open.current = onOpen;

  useEffect(() => {
    const m = L.map(host.current, { zoomControl: true, attributionControl: true, scrollWheelZoom: true, minZoom: 5, maxZoom: 17 });
    L.tileLayer(IMAGERY, { attribution: "Imagery © Esri, Maxar, Earthstar Geographics", maxZoom: 19 }).addTo(m);
    L.tileLayer(PLACES, { maxZoom: 19, opacity: 0.9 }).addTo(m);
    layer.current = L.layerGroup().addTo(m);
    m.setView([12.8, 77.75], 10);
    map.current = m;
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(host.current);
    return () => { ro.disconnect(); m.remove(); map.current = null; };
  }, []);

  useEffect(() => {
    const m = map.current;
    if (!m || !sites?.length) return;
    layer.current.clearLayers();
    const pts = sites.filter(s => s.lat != null && s.lon != null);
    pts.forEach(s => {
      const el = card(s);
      el.addEventListener("click", () => open.current(s.plant_id));
      const icon = L.divIcon({ className: "pm-pin", html: el, iconSize: null, iconAnchor: [0, 0] });
      L.marker([s.lat, s.lon], { icon, keyboard: true, riseOnHover: true, title: `${s.city} · ${s.plant_id}` }).addTo(layer.current);
    });
    if (pts.length && !m._fitted) {
      m.fitBounds(L.latLngBounds(pts.map(s => [s.lat, s.lon])).pad(0.9), { maxZoom: 11 });
      m._fitted = true;
    }
  }, [sites]);

  return <div className="pm-map" ref={host} role="application" aria-label="Satellite map of the plants. Each plant is a card you can open." />;
}
