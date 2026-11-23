"""Builds the interactive architecture map (frontend/public/arch-graph.html) from the graphify knowledge graph.

graphify-out/graph.json holds every function, class and import graphify found in this app. This script rolls those up to
architecture components (one per group of files below), counts the real links graphify found between components, adds the
screens' calls to the API (read from the api("/api/...") paths in the UI) and the outside systems each component talks to.
Rerun after `graphify` updates the graph:  python docs/arch_graph.py
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
G = json.loads((ROOT / "graphify-out" / "graph.json").read_text(encoding="utf-8"))

# layer, name, plain description, files (by name). Order of layers = top to bottom.
LAYERS = [("people", "Screens people use"), ("api", "App server and API"), ("agents", "Agents"),
          ("engine", "Planning engine"), ("data", "Data and outside systems")]
COMP = [
    ("people", "Overview and dashboards", "The first page, the dashboard and its charts.",
     "Overview.jsx Dashboard.jsx dzcharts.jsx charts.jsx Drill.jsx AnswerChart.jsx widgets.jsx ui.jsx views.jsx"),
    ("people", "Plan and review screens", "The plan, its timeline and processing order, review and approval, what-if.",
     "Planner.jsx PlanningViews.jsx PlanPages.jsx RunOrder.jsx OrderIntake.jsx PlanAhead.jsx Filters.jsx"),
    ("people", "Digital twin screens", "The 3D plant, the plant map and every machine, line and warehouse.",
     "TwinView.jsx Floor3D.jsx floorModels.js PlantMap.jsx PlantArt.jsx motion.js"),
    ("people", "Connections, team and users", "Purchase-order uploads, data connections, the agent team and user accounts.",
     "IntegrationsView.jsx UsersView.jsx AgentView.jsx AgentTeam.jsx Problems.jsx"),
    ("people", "Planning assistant chat", "Ask questions about the plan in plain words.", "Assistant.jsx AssistantCard.jsx AssistantFab.jsx"),
    ("people", "App shell and sign-in", "Navigation, sign-in and the link to the server.", "App.jsx api.js Login.jsx Brand.jsx main.jsx"),
    ("api", "Planning API", "Plans, approvals, sign-off, insights, the twin and planning tools.", "planning_api.py schemas.py errors.py"),
    ("api", "Agent API", "Uploads, agent runs, tasks, floor problems and notifications.", "agent_api.py"),
    ("api", "Connections API", "Data connections, imports and the Neon table browser.", "integrations_api.py connectors.py integrations.py"),
    ("api", "Sign-in and roles", "Accounts, sessions and what each role may do.", "auth.py users.py"),
    ("api", "App server", "Starts everything and serves the screens.", "main.py"),
    ("agents", "Orchestrator", "Hears every signal and sends it to the agent that should act.", "orchestrator.py"),
    ("agents", "Order watcher", "Checks new purchase orders and starts the agents.", "order_watcher.py intake_flow.py"),
    ("agents", "Planning agent", "Builds and explains the plan.", "planning_agent.py"),
    ("agents", "Inventory agent", "Checks materials against the plan and safety stock.", "inventory_agent.py"),
    ("agents", "Sourcing agent", "Suggests what to buy, and from whom, when materials run short.", "sourcing_agent.py"),
    ("agents", "Floor agent", "Watches machines and crews and raises problems.", "floor_agent.py"),
    ("agents", "Forecast agent", "Forecasts the coming weeks with the method that worked best.", "forecast_agent.py"),
    ("agents", "Planning assistant", "Answers questions with the planning tools, checked for safety.", "agent.py tools.py"),
    ("engine", "Planning engine", "Finite-capacity planning, scenarios and planning tools.", "planning.py domain.py scenarios.py planning_tools.py rework.py"),
    ("engine", "Insights and twin", "Facts from the data and the plant at any hour.", "insights.py twin.py"),
    ("engine", "Plan store", "Saves plans, approvals and the audit trail.", "store.py"),
    ("engine", "Sign-off and email", "Approval stages, the approved-plan email and notifications.", "signoff.py notify.py publish.py"),
    ("engine", "Floor problems", "Machine and line problems and their fixes.", "incidents.py"),
    ("data", "Neon database", "The shared database every agent reads from and writes to.", "db.py neon_mcp.py data.py config.py generate.py"),
]
EXTERNAL = [  # outside systems: (name, description, talked to by)
    ("ERP and SQL databases", "SAP, PostgreSQL, MySQL, SQL Server and Oracle connections.", "Connections API"),
    ("Domo AI", "The language model behind the assistant and agent notes.", "Planning assistant"),
    ("NVIDIA NemoGuard", "Checks questions and answers for safety.", "Planning assistant"),
    ("Email (SMTP)", "Sends tasks and the approved plan to the team.", "Sign-off and email"),
]
EXT_FILE = {"domo_llm.py": "Domo AI", "nemoguard.py": "NVIDIA NemoGuard"}   # code that only wraps an outside system
API_OF = {"agent": "Agent API", "orders": "Agent API", "notifications": "Agent API", "integrations": "Connections API",
          "auth": "Sign-in and roles", "users": "Sign-in and roles", "chat": "App server"}           # anything else: Planning API

file_comp = {f: c[1] for c in COMP for f in c[3].split()}
node_comp, size = {}, Counter()
for n in G["nodes"]:
    f = Path(str(n.get("source_file") or "")).name
    c = file_comp.get(f) or EXT_FILE.get(f)
    if c and "test" not in str(n.get("source_file")):
        node_comp[n["id"]] = c
        size[c] += 1

links, kinds = Counter(), defaultdict(Counter)
for e in G["links"]:
    a, b = node_comp.get(e["source"]), node_comp.get(e["target"])
    if a and b and a != b:
        links[(a, b)] += 1
        kinds[(a, b)][e.get("relation", "")] += 1

# screens -> API: the api("/api/...") calls in each screen file
for c in COMP:
    if c[0] != "people":
        continue
    for f in c[3].split():
        p = next(ROOT.glob(f"frontend/src/**/{f}"), None)
        for m in re.findall(r'/api/([a-z-]+)', p.read_text(encoding="utf-8") if p else ""):
            links[(c[1], API_OF.get(m, "Planning API"))] += 1
            kinds[(c[1], API_OF.get(m, "Planning API"))]["calls the API"] += 1
for name, _, by in EXTERNAL:
    links.setdefault((by, name), 1)
    kinds[(by, name)]["talks to"] += 1

# one arrow per pair, pointing the way most links go
edges = []
for a, b in sorted({tuple(sorted(k)) for k in links}):
    ab, ba = links.get((a, b), 0), links.get((b, a), 0)
    src, dst = (a, b) if ab >= ba else (b, a)
    edges.append({"from": src, "to": dst, "n": ab + ba, "how": dict(kinds[(a, b)] + kinds[(b, a)])})

comms = Counter((n.get("community_name"), node_comp.get(n["id"])) for n in G["nodes"] if node_comp.get(n["id"]))
groups = defaultdict(list)
for (cn, c), k in comms.most_common():
    if cn and len(groups[c]) < 4:
        groups[c].append(cn)

nodes = [{"id": c[1], "layer": c[0], "desc": c[2], "parts": size[c[1]], "groups": groups[c[1]]} for c in COMP]
nodes += [{"id": n, "layer": "data", "desc": d, "parts": size[n], "external": True, "groups": []} for n, d, _ in EXTERNAL]
data = {"layers": LAYERS, "nodes": nodes, "edges": edges,
        "source": {"graph_nodes": len(G["nodes"]), "graph_links": len(G["links"]), "communities": len({n.get("community") for n in G["nodes"]})}}

tpl = (ROOT / "docs" / "arch_graph_template.html").read_text(encoding="utf-8")
(ROOT / "frontend" / "public" / "arch-graph.html").write_text(tpl.replace("/*DATA*/null", json.dumps(data, ensure_ascii=False)), encoding="utf-8")
print(f"{len(nodes)} components, {len(edges)} connections, from {len(G['nodes'])} graphify nodes")
