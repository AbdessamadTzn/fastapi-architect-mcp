"""Standalone interactive HTML view of a knowledge graph (vis-network)."""
import json
from collections import Counter

from fastapi_architect.graph.audit import is_test_file
from fastapi_architect.graph.model import FUNCTION_TYPES, EdgeType, KnowledgeGraph, NodeType

VIS_NETWORK_URL = "https://cdn.jsdelivr.net/npm/vis-network@9.1.9/standalone/umd/vis-network.min.js"

NODE_STYLE: dict[NodeType, tuple[str, str]] = {  # type → (color, vis shape)
    NodeType.APP: ("#facc15", "star"),
    NodeType.ROUTER: ("#f59e0b", "hexagon"),
    NodeType.ROUTE: ("#ef4444", "diamond"),
    NodeType.HANDLER: ("#fb923c", "dot"),
    NodeType.DEPENDENCY: ("#a78bfa", "triangle"),
    NodeType.MIDDLEWARE: ("#e879f9", "triangle"),
    NodeType.SCHEMA: ("#60a5fa", "dot"),
    NodeType.ORM_MODEL: ("#34d399", "square"),
    NodeType.TABLE: ("#2dd4bf", "square"),
    NodeType.TEMPLATE: ("#f472b6", "triangleDown"),
    NodeType.FUNCTION: ("#94a3b8", "dot"),
    NodeType.CLASS: ("#cbd5e1", "dot"),
    NodeType.MODULE: ("#64748b", "ellipse"),
}

# nodes shown by default; plain functions/classes only when they touch one of these
CORE_TYPES = set(NodeType) - {NodeType.FUNCTION, NodeType.CLASS, NodeType.MODULE}


def graph_payload(kg: KnowledgeGraph) -> dict:
    degree: Counter[str] = Counter()
    for s, t, d in kg.g.edges(data=True):
        if d["type"] != EdgeType.DEFINES:
            degree[s] += 1
            degree[t] += 1

    def is_core(node_id: str) -> bool:
        node_type = kg.type_of(node_id)
        if node_type in CORE_TYPES:
            return True
        if node_type == NodeType.MODULE:
            return False
        neighbors = [
            other
            for other, d in [*kg.out_edges(node_id), *kg.in_edges(node_id)]
            if d["type"] != EdgeType.DEFINES
        ]
        return any(kg.type_of(n) in CORE_TYPES for n in neighbors)

    nodes = []
    for node_id, data in kg.g.nodes(data=True):
        node_type = data["type"]
        node = {
            "id": node_id,
            "label": data["name"],
            "type": str(node_type),
            "degree": degree[node_id],
            "core": is_core(node_id),
            "test": is_test_file(data.get("file", "")),
            "external": bool(data.get("external")),
        }
        for key in ("file", "line", "full_paths", "method", "tablename", "fields"):
            if key in data:
                node[key] = data[key]
        if node_type in FUNCTION_TYPES and data.get("is_async"):
            node["async"] = True
        nodes.append(node)

    edges = []
    for s, t, data in kg.g.edges(data=True):
        edge = {"from": s, "to": t, "type": str(data["type"])}
        for key in ("prefix", "ops", "confidence", "via", "param"):
            if key in data:
                edge[key] = data[key]
        edges.append(edge)

    return {
        "nodes": nodes,
        "edges": edges,
        "styles": {str(t): {"color": c, "shape": s} for t, (c, s) in NODE_STYLE.items()},
        "edgeTypes": [str(e) for e in EdgeType],
    }


def render_html(kg: KnowledgeGraph, title: str) -> str:
    payload = json.dumps(graph_payload(kg), separators=(",", ":")).replace("</", "<\\/")
    return (
        _TEMPLATE
        .replace("__TITLE__", _escape(title))
        .replace("__VIS_URL__", VIS_NETWORK_URL)
        .replace("__DATA__", payload)
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<script src="__VIS_URL__"></script>
<style>
  :root {
    --bg: #0f1117; --panel: #161a23; --border: #262b36; --text: #e2e8f0; --muted: #8b93a3;
    --accent: #60a5fa; --hover: #1e2330;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; background: var(--bg); color: var(--text);
    font: 13px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
  #app { display: flex; height: 100vh; }
  #graph { flex: 1; min-width: 0; position: relative; }
  #network { position: absolute; inset: 0; }
  #status { position: absolute; left: 12px; bottom: 10px; color: var(--muted); font-size: 12px; pointer-events: none; }
  aside { width: 340px; max-width: 45vw; border-left: 1px solid var(--border); background: var(--panel);
    display: flex; flex-direction: column; overflow: hidden; }
  aside header { padding: 14px 16px 10px; border-bottom: 1px solid var(--border); }
  aside h1 { font-size: 14px; margin: 0 0 8px; font-weight: 600; }
  input[type=search] { width: 100%; padding: 7px 9px; border-radius: 6px; border: 1px solid var(--border);
    background: var(--bg); color: var(--text); font: inherit; }
  .scroll { overflow-y: auto; flex: 1; }
  section { padding: 12px 16px; border-bottom: 1px solid var(--border); }
  h2 { font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); margin: 0 0 8px; font-weight: 600; }
  label.row { display: flex; align-items: center; gap: 8px; padding: 2px 0; cursor: pointer; }
  label.row .count { margin-left: auto; color: var(--muted); font-variant-numeric: tabular-nums; }
  .swatch { width: 10px; height: 10px; border-radius: 2px; flex: none; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; column-gap: 12px; }
  #details .name { font-weight: 600; font-size: 14px; word-break: break-word; }
  #details .meta { color: var(--muted); margin: 4px 0 10px; word-break: break-all; }
  #details .empty { color: var(--muted); }
  .group { margin-top: 10px; }
  .group-title { color: var(--muted); font-size: 11px; margin-bottom: 3px; }
  .link { display: block; width: 100%; text-align: left; background: none; border: 0; color: var(--text);
    padding: 3px 6px; border-radius: 4px; cursor: pointer; font: inherit; border-left: 3px solid transparent; }
  .link:hover { background: var(--hover); }
  .link small { color: var(--muted); }
  .suggestions { margin-top: 6px; }
  code { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
  button.plain { background: var(--bg); color: var(--text); border: 1px solid var(--border); border-radius: 6px;
    padding: 5px 9px; font: inherit; cursor: pointer; }
  button.plain:hover { background: var(--hover); }
  @media (max-width: 720px) {
    #app { flex-direction: column; }
    #graph { flex: 1 1 55%; }
    aside { width: auto; max-width: none; flex: 1 1 45%; border-left: 0; border-top: 1px solid var(--border); }
  }
</style>
</head>
<body>
<div id="app">
  <div id="graph"><div id="network"></div><div id="status"></div></div>
  <aside>
    <header>
      <h1>__TITLE__</h1>
      <input id="search" type="search" placeholder="Search a route, handler, model, table…" autocomplete="off">
      <div id="suggestions" class="suggestions"></div>
    </header>
    <div class="scroll">
      <section>
        <h2>Selection</h2>
        <div id="details"><div class="empty">Click a node to see its connections.</div></div>
      </section>
      <section>
        <h2>View</h2>
        <label class="row"><input type="checkbox" id="opt-all-functions"> Show all functions &amp; classes</label>
        <label class="row"><input type="checkbox" id="opt-tests"> Show test files</label>
        <label class="row"><input type="checkbox" id="opt-modules"> Show modules</label>
        <label class="row"><input type="checkbox" id="opt-physics" checked> Physics</label>
        <div style="margin-top:8px"><button class="plain" id="fit">Fit to screen</button></div>
      </section>
      <section>
        <h2>Node types</h2>
        <div id="node-types"></div>
      </section>
      <section>
        <h2>Edge types</h2>
        <div id="edge-types" class="grid"></div>
      </section>
    </div>
  </aside>
</div>
<script>
const DATA = __DATA__;
const byId = new Map(DATA.nodes.map(n => [n.id, n]));
const hidden = { types: new Set(["Module"]), edges: new Set(["DEFINES"]) };
const opts = { allFunctions: false, tests: false, modules: false };
let selected = null;

const statusEl = document.getElementById("status");
if (typeof vis === "undefined") {
  statusEl.textContent = "vis-network could not be loaded (offline?). The graph view needs internet access.";
}

function nodeVisible(n) {
  if (hidden.types.has(n.type)) return false;
  if (n.test && !opts.tests) return false;
  if (!n.core && !opts.allFunctions) return false;
  return true;
}

function toVisNode(n) {
  const style = DATA.styles[n.type];
  const size = 8 + Math.min(22, Math.sqrt(n.degree) * 3);
  return {
    id: n.id, label: n.label, shape: style.shape, size,
    color: { background: style.color, border: style.color, highlight: { background: "#fff", border: style.color } },
    font: { color: "#cbd5e1", size: 11, strokeWidth: 3, strokeColor: "#0f1117" },
    title: n.type + " · " + n.label,
  };
}

function toVisEdge(e, i) {
  return {
    id: i, from: e.from, to: e.to, arrows: { to: { enabled: true, scaleFactor: 0.4 } },
    color: { color: edgeColor(e.type), opacity: 0.45, highlight: "#fff" }, width: 1,
    title: e.type + (e.prefix ? " prefix=" + e.prefix : "") + (e.ops ? " " + e.ops.join("/") : "")
      + (e.confidence !== undefined ? " confidence=" + e.confidence : ""),
  };
}

const EDGE_COLORS = {
  HANDLED_BY: "#fb923c", HAS_ROUTE: "#ef4444", INCLUDES: "#f59e0b", DEPENDS_ON: "#a78bfa",
  ACCEPTS: "#60a5fa", RETURNS: "#60a5fa", QUERIES: "#2dd4bf", MAPS_TO: "#34d399", REFERENCES: "#34d399",
  RELATES_TO: "#34d399", MIRRORS: "#93c5fd", RENDERS: "#f472b6", MIDDLEWARE: "#e879f9",
};
function edgeColor(type) { return EDGE_COLORS[type] || "#64748b"; }

const nodes = new vis.DataSet(DATA.nodes.map(toVisNode));
const edges = new vis.DataSet(DATA.edges.map(toVisEdge));
const nodeView = new vis.DataView(nodes, { filter: v => nodeVisible(byId.get(v.id)) });
const edgeView = new vis.DataView(edges, { filter: v => !hidden.edges.has(DATA.edges[v.id].type) });

const network = new vis.Network(document.getElementById("network"), { nodes: nodeView, edges: edgeView }, {
  physics: {
    solver: "forceAtlas2Based",
    forceAtlas2Based: { gravitationalConstant: -38, springLength: 90, springConstant: 0.06, avoidOverlap: 0.2 },
    stabilization: { iterations: 250, updateInterval: 25 },
  },
  interaction: { hover: true, tooltipDelay: 150, hideEdgesOnDrag: true },
  edges: { smooth: false },
  layout: { improvedLayout: false },
});
network.on("stabilizationIterationsDone", () => {
  network.setOptions({ physics: { enabled: document.getElementById("opt-physics").checked } });
  updateStatus();
  selectFromHash();
});

// deep link: graph.html#node=<id>
function selectFromHash() {
  const match = location.hash.match(/^#node=(.+)$/);
  if (match && byId.has(decodeURIComponent(match[1]))) select(decodeURIComponent(match[1]), true);
}
window.addEventListener("hashchange", selectFromHash);

function refresh() {
  nodeView.refresh();
  edgeView.refresh();
  if (selected && !nodeView.get(selected)) clearSelection();
  updateStatus();
}

function updateStatus() {
  statusEl.textContent = nodeView.length + " / " + DATA.nodes.length + " nodes · " + edgeView.length + " edges";
}

// ─── filters ─────────────────────────────────────────────────────────────────
const typeCounts = {};
DATA.nodes.forEach(n => { typeCounts[n.type] = (typeCounts[n.type] || 0) + 1; });
const nodeTypesEl = document.getElementById("node-types");
Object.keys(DATA.styles).filter(t => typeCounts[t]).forEach(type => {
  const row = document.createElement("label");
  row.className = "row";
  row.innerHTML = `<input type="checkbox" ${hidden.types.has(type) ? "" : "checked"}>
    <span class="swatch" style="background:${DATA.styles[type].color}"></span>${type}
    <span class="count">${typeCounts[type]}</span>`;
  row.querySelector("input").addEventListener("change", ev => {
    ev.target.checked ? hidden.types.delete(type) : hidden.types.add(type);
    if (type === "Module") document.getElementById("opt-modules").checked = ev.target.checked;
    refresh();
  });
  row.dataset.type = type;
  nodeTypesEl.appendChild(row);
});

const edgeCounts = {};
DATA.edges.forEach(e => { edgeCounts[e.type] = (edgeCounts[e.type] || 0) + 1; });
const edgeTypesEl = document.getElementById("edge-types");
DATA.edgeTypes.filter(t => edgeCounts[t]).forEach(type => {
  const row = document.createElement("label");
  row.className = "row";
  row.innerHTML = `<input type="checkbox" ${hidden.edges.has(type) ? "" : "checked"}>
    <span class="swatch" style="background:${edgeColor(type)}"></span><code>${type}</code>`;
  row.title = edgeCounts[type] + " edges";
  row.querySelector("input").addEventListener("change", ev => {
    ev.target.checked ? hidden.edges.delete(type) : hidden.edges.add(type);
    refresh();
  });
  edgeTypesEl.appendChild(row);
});

document.getElementById("opt-all-functions").addEventListener("change", ev => { opts.allFunctions = ev.target.checked; refresh(); });
document.getElementById("opt-tests").addEventListener("change", ev => { opts.tests = ev.target.checked; refresh(); });
document.getElementById("opt-modules").addEventListener("change", ev => {
  ev.target.checked ? hidden.types.delete("Module") : hidden.types.add("Module");
  const box = nodeTypesEl.querySelector('[data-type="Module"] input');
  if (box) box.checked = ev.target.checked;
  refresh();
});
document.getElementById("opt-physics").addEventListener("change", ev => network.setOptions({ physics: { enabled: ev.target.checked } }));
document.getElementById("fit").addEventListener("click", () => network.fit({ animation: true }));

// ─── selection ───────────────────────────────────────────────────────────────
const detailsEl = document.getElementById("details");

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function select(id, focus) {
  const n = byId.get(id);
  if (!n) return;
  if (!nodeVisible(n)) {  // reveal what the user asked for
    hidden.types.delete(n.type);
    if (n.test) { opts.tests = true; document.getElementById("opt-tests").checked = true; }
    if (!n.core) { opts.allFunctions = true; document.getElementById("opt-all-functions").checked = true; }
    const box = nodeTypesEl.querySelector(`[data-type="${n.type}"] input`);
    if (box) box.checked = true;
    refresh();
  }
  selected = id;
  history.replaceState(null, "", "#node=" + encodeURIComponent(id));
  network.selectNodes([id]);
  if (focus) network.focus(id, { scale: 1.2, animation: { duration: 400 } });

  const outgoing = {}, incoming = {};
  DATA.edges.forEach(e => {
    if (e.type === "DEFINES") return;
    if (e.from === id) (outgoing[e.type] ||= []).push({ id: e.to, edge: e });
    if (e.to === id) (incoming[e.type] ||= []).push({ id: e.from, edge: e });
  });

  const meta = [n.type];
  if (n.file) meta.push(n.file + (n.line ? ":" + n.line : ""));
  let html = `<div class="name">${escapeHtml(n.label)}</div><div class="meta">${meta.map(escapeHtml).join(" · ")}</div>`;
  if (n.full_paths && n.full_paths.length > 1) html += `<div class="meta">Paths: ${n.full_paths.map(escapeHtml).join(", ")}</div>`;
  if (n.fields && n.fields.length) html += `<div class="meta">Fields: ${n.fields.map(escapeHtml).join(", ")}</div>`;

  const renderGroups = (groups, arrow) => Object.keys(groups).sort().map(type => {
    const items = groups[type].map(({ id: other, edge }) => {
      const o = byId.get(other);
      const extra = edge.prefix ? ` prefix=${edge.prefix}` : edge.ops ? ` ${edge.ops.join("/")}` : edge.confidence !== undefined ? ` ${edge.confidence}` : "";
      return `<button class="link" data-id="${escapeHtml(other)}" style="border-left-color:${DATA.styles[o.type].color}">
        ${escapeHtml(o.label)} <small>${o.type}${escapeHtml(extra)}</small></button>`;
    }).join("");
    return `<div class="group"><div class="group-title">${arrow} ${type} (${groups[type].length})</div>${items}</div>`;
  }).join("");

  const body = renderGroups(outgoing, "→") + renderGroups(incoming, "←");
  detailsEl.innerHTML = html + (body || '<div class="empty">No connections.</div>');
  detailsEl.querySelectorAll(".link").forEach(b => b.addEventListener("click", () => select(b.dataset.id, true)));
}

function clearSelection() {
  selected = null;
  history.replaceState(null, "", location.pathname + location.search);
  network.unselectAll();
  detailsEl.innerHTML = '<div class="empty">Click a node to see its connections.</div>';
}

network.on("click", params => params.nodes.length ? select(params.nodes[0], false) : clearSelection());

// ─── search ──────────────────────────────────────────────────────────────────
const searchEl = document.getElementById("search");
const suggestionsEl = document.getElementById("suggestions");
searchEl.addEventListener("input", () => {
  const q = searchEl.value.trim().toLowerCase();
  if (!q) { suggestionsEl.innerHTML = ""; return; }
  const matches = DATA.nodes
    .filter(n => n.type !== "Module" && (n.label.toLowerCase().includes(q) || n.id.toLowerCase().includes(q)))
    .sort((a, b) => (a.label.toLowerCase().startsWith(q) ? 0 : 1) - (b.label.toLowerCase().startsWith(q) ? 0 : 1) || b.degree - a.degree)
    .slice(0, 8);
  suggestionsEl.innerHTML = matches.map(n =>
    `<button class="link" data-id="${escapeHtml(n.id)}" style="border-left-color:${DATA.styles[n.type].color}">
      ${escapeHtml(n.label)} <small>${n.type}${n.file ? " · " + escapeHtml(n.file) : ""}</small></button>`).join("");
  suggestionsEl.querySelectorAll(".link").forEach(b => b.addEventListener("click", () => {
    select(b.dataset.id, true);
    suggestionsEl.innerHTML = "";
    searchEl.value = "";
  }));
});
searchEl.addEventListener("keydown", ev => {
  if (ev.key === "Enter") { const first = suggestionsEl.querySelector(".link"); if (first) first.click(); }
  if (ev.key === "Escape") { suggestionsEl.innerHTML = ""; searchEl.value = ""; }
});

updateStatus();
</script>
</body>
</html>
"""
