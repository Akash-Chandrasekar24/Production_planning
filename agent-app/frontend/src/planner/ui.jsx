// Small display helpers. Everything shown comes from the API; nothing here computes feasibility.
export const N = v => (v == null ? "–" : typeof v === "number" ? v.toLocaleString(undefined, { maximumFractionDigits: 2 }) : String(v));
export const dt = v => (v ? String(v).slice(0, 16).replace("T", " ") : "–");

export const FEAS = {
  NO_NEW_PRODUCTION_NEEDED: ["warn", "No new production needed. Open demand is covered by what open production orders still have to make (production confirmations)."],
  COMPLETE_ON_TIME: ["ok", "All demand is scheduled and meets its due dates (within the constraints listed as not enforced)."],
  COMPLETE_WITH_LATE_RISK: ["warn", "All demand is scheduled, but some jobs finish after their due date."],
  INCOMPLETE: ["bad", "Not all demand can be scheduled. See unscheduled demand and the reasons."],
  NOTHING_SCHEDULABLE: ["bad", "Nothing can be scheduled (see reasons: material, capacity or missing data)."],
};
const CODES = {
  PLANT_BEST_COVER_THEN_EARLIEST_FINISH: "Plant chosen because it could cover the most units from available material, then finish earliest.",
  CHANGEOVER_ADDED: "Setup time was added because the product on this line changed.",
  PARTIAL_MATERIAL: "Only part of the order is scheduled: available material covers less than the order quantity.",
  LATE_RISK: "Finishes after the due date.", PAST_DUE_AT_PLAN_START: "The due date was already past when the plan starts.",
  FROZEN: "Job in the frozen zone (first 2 weeks): kept exactly where the previous plan put it.",
  FROZEN_FENCE_BROKEN: "An expedited order was put ahead of the frozen zone: frozen jobs may move, so management must approve it.",
  SLUSHY_ZONE_CHANGES: "Orders in weeks 3–6 changed: check the material and capacity impact before approving.",
  PLANNED_AFTER_PURCHASE: "Stock doesn't cover these units: they start once the parts arrive, if they are ordered today.",
  BOTTLENECK_LEVELLED: "Sent to the plant whose final assembly (the bottleneck) is less loaded, while still on time.",
  SPLIT_FOR_PURCHASE: "Part of the order is made from stock now; the rest after the missing parts arrive.",
  FORECAST_REPLENISHMENT: "Made for stock from the demand forecast, after all customer orders.",
  FORECAST_NOT_PLANNED: "Forecast demand that doesn't fit: noted only, it is not a customer order.",
  MATERIAL_BLOCKED: "No available material for this product, so nothing can start.", MATERIAL_SHORT: "Part of the quantity is short of material.",
  NO_CAPACITY_IN_HORIZON: "Does not fit in the planning horizon on the available line capacity.", MISSING_ROUTING: "No routing for this product: cannot be planned (nothing assumed).",
  MISSING_BOM: "No effective BOM for this product: cannot be planned (nothing assumed).", OPEN_PO_SUPPLY_APPLIED: "Demand reduced by what open production orders still have to make.",
  FROZEN_JOB_MOVED: "A frozen job had to move because capacity was lost.", FROZEN_JOB_UNPLACEABLE: "A frozen job could not be placed after capacity loss.",
  MATERIAL_SHORT_FROZEN: "A frozen job needs more material than is available.", MATERIAL_SHORT_PARTIAL: "Material covers only part of this order.",
};
export const codeHelp = c =>
  c.startsWith("WAITS_FOR_PURCHASE") ? `Starts when ${c.split(":")[1]} arrives: order it today (supplier lead time).` : c.startsWith("PO_NETTED") ? `Part of this order is covered by open production orders: ${c.split(":")[1]} units.` : CODES[c] || (c.startsWith("DATA_") ? "Data issue found in the source data." : "");

const LABELS = {
  PLANT_BEST_COVER_THEN_EARLIEST_FINISH: "Best plant", CHANGEOVER_ADDED: "Changeover", PARTIAL_MATERIAL: "Partly covered", LATE_RISK: "Late",
  PAST_DUE_AT_PLAN_START: "Already overdue", FROZEN: "Frozen", MATERIAL_BLOCKED: "No material", MATERIAL_SHORT: "Short of material",
  NO_CAPACITY_IN_HORIZON: "No capacity", MISSING_ROUTING: "No routing", MISSING_BOM: "No bill of materials", OPEN_PO_SUPPLY_APPLIED: "Covered by production orders",
  FROZEN_JOB_MOVED: "Frozen job moved", FROZEN_JOB_UNPLACEABLE: "Frozen job not placed", MATERIAL_SHORT_FROZEN: "Frozen job short", MATERIAL_SHORT_PARTIAL: "Partly covered",
  FROZEN_FENCE_BROKEN: "Frozen zone broken", SLUSHY_ZONE_CHANGES: "Changes in weeks 3–6", PLANNED_AFTER_PURCHASE: "Waits for parts",
  BOTTLENECK_LEVELLED: "Bottleneck levelled", SPLIT_FOR_PURCHASE: "Split: stock, then bought parts", FORECAST_REPLENISHMENT: "Forecast (stock)",
  FORECAST_NOT_PLANNED: "Forecast not planned",
};
export const label = c => (c.startsWith("WAITS_FOR_PURCHASE") ? "Waits for parts" : c.startsWith("PO_NETTED") ? "Covered by production orders" : c.startsWith("DATA_") ? "Data note"
  : LABELS[c] || c.toLowerCase().replace(/_/g, " ").replace(/^./, x => x.toUpperCase()));
export const Badge = ({ c }) => <span className="badge" title={codeHelp(c)}>{label(c)}</span>;

const DQ = {
  PO_CO_LINK_BROKEN: "Production orders can't be matched to customer orders, so supply can't be traced to specific orders.",
  HOURLY_PO_MISMATCH: "Hourly production records often name a different product or plant than their production order; they are only used to measure line speed.",
  MACHINE_PLANT_MISMATCH: "Some machines are recorded in a different plant than their production line.",
  STATUS_SHIPPED_INCONSISTENT: "Some open or confirmed orders already show shipped quantities; the open quantity is ordered minus shipped.",
  INV_STATUS_VS_HOLD: "Stock marked as available sometimes includes quality-hold quantities; only the quantities are used.",
  INV_PLANT_UNSTABLE: "Stock is recorded under a different plant from day to day, so stock is treated as shared between plants.",
  CALENDAR_ENDS_BEFORE_ASOF: "The shift calendar ends before the plan starts; standard 3-shift days are assumed after it.",
  ORDER_NO_OPEN_QTY: "Some open orders have nothing left to ship and are skipped.",
};
export const plainDq = x => DQ[x.code] || x.message;
export const Card = ({ l, v, cls = "", note }) => (
  <div className="card"><small>{l}</small><b className={cls}>{N(v)}</b>{note && <small>{note}</small>}</div>
);

// cols: { h, f(row) -> text|number|node, num?, cls?(row) }
export function Table({ cap, cols, rows, empty = "Nothing to show." }) {
  if (!rows.length) return <p className="muted">{empty}</p>;
  return (
    <div className="tw">
      <table>
        <caption>{cap}</caption>
        <thead><tr>{cols.map((c, i) => <th key={i} scope="col" className={c.num ? "num" : ""}>{c.h}</th>)}</tr></thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              {cols.map((c, j) => {
                const v = c.f(r);
                return <td key={j} className={(c.num ? "num " : "") + (c.cls ? c.cls(r) : "")}>{typeof v === "number" || v == null ? N(v) : v}</td>;
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function Head({ s }) {
  const f = FEAS[s.feasibility] || ["", s.feasibility];
  return (
    <div className="panel">
      <b>{s.name}</b> <span className="muted">(plan start {dt(s.as_of).slice(0, 10)}, created by {s.created_by})</span>{" "}
      <span className="badge">{s.status.replace(/_/g, " ")}</span>
      <p className={f[0]} style={{ margin: "6px 0 0" }}><b>{s.feasibility.replace(/_/g, " ")}.</b> {f[1]}</p>
    </div>
  );
}

export function CapacityBars({ gaps }) {
  const rows = gaps.by_line_week;
  const weeks = [...new Set(rows.map(r => r.week))], lines = [...new Set(rows.map(r => r.line_id))];
  return (
    <div className="tw">
      <table>
        <caption>Utilisation by line and week (used / available minutes)</caption>
        <thead><tr><th scope="col">Week</th>{lines.map(l => <th key={l} scope="col">{l}</th>)}</tr></thead>
        <tbody>
          {weeks.map(w => (
            <tr key={w}>
              <th scope="row">{w}</th>
              {lines.map(l => {
                const r = rows.find(x => x.week === w && x.line_id === l), u = r?.utilisation ?? 0;
                return (
                  <td key={l}>
                    <div className="bar" role="img" aria-label={`${l} ${w} ${(u * 100).toFixed(0)} percent`}><i className={u >= 0.95 ? "hot" : ""} style={{ width: `${Math.min(100, u * 100)}%` }} /></div>
                    <small>{(u * 100).toFixed(0)}% · {N(r?.used_min)}/{N(r?.available_min)}</small>
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
