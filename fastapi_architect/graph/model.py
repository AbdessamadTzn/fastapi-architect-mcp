"""Typed knowledge graph for FastAPI projects, backed by a networkx MultiDiGraph."""
from collections import Counter
from enum import StrEnum
from typing import Any

import networkx as nx


class NodeType(StrEnum):
    MODULE = "Module"
    APP = "App"
    ROUTER = "Router"
    ROUTE = "Route"
    HANDLER = "Handler"
    DEPENDENCY = "Dependency"
    MIDDLEWARE = "Middleware"
    FUNCTION = "Function"
    SCHEMA = "Schema"
    ORM_MODEL = "ORMModel"
    CLASS = "Class"
    TABLE = "Table"
    TEMPLATE = "Template"


class EdgeType(StrEnum):
    DEFINES = "DEFINES"            # Module/Class → symbol
    INCLUDES = "INCLUDES"          # App/Router → Router            (attr: prefix)
    HAS_ROUTE = "HAS_ROUTE"        # App/Router → Route
    HANDLED_BY = "HANDLED_BY"      # Route → Handler
    DEPENDS_ON = "DEPENDS_ON"      # Route/Router/function → Dependency  (attr: via)
    ACCEPTS = "ACCEPTS"            # function → Schema/ORMModel     (attr: param)
    RETURNS = "RETURNS"            # function → Schema/ORMModel
    CALLS = "CALLS"                # function → function
    USES = "USES"                  # function/class/Module → class  (attr: via="field" for annotations)
    QUERIES = "QUERIES"            # function/Module → Table        (attr: ops)
    RENDERS = "RENDERS"            # function → Template
    MIDDLEWARE = "MIDDLEWARE"      # App → Middleware
    INHERITS = "INHERITS"          # class → class
    MAPS_TO = "MAPS_TO"            # ORMModel → Table
    REFERENCES = "REFERENCES"      # ORMModel → Table (ForeignKey)
    RELATES_TO = "RELATES_TO"      # ORMModel → ORMModel (relationship)
    MIRRORS = "MIRRORS"            # Schema → ORMModel              (attr: confidence)


FUNCTION_TYPES = {NodeType.HANDLER, NodeType.DEPENDENCY, NodeType.MIDDLEWARE, NodeType.FUNCTION}
CLASS_TYPES = {NodeType.SCHEMA, NodeType.ORM_MODEL, NodeType.CLASS}
MODEL_TYPES = {NodeType.SCHEMA, NodeType.ORM_MODEL}


class KnowledgeGraph:
    """Thin wrapper over nx.MultiDiGraph: one edge per (source, target, type)."""

    def __init__(self) -> None:
        self.g = nx.MultiDiGraph()

    # ─── mutation ────────────────────────────────────────────────────────────

    def add_node(self, node_id: str, type: NodeType, name: str, **attrs: Any) -> str:
        if node_id in self.g:
            self.g.nodes[node_id].update(type=type, name=name, **attrs)
        else:
            self.g.add_node(node_id, type=type, name=name, **attrs)
        return node_id

    def add_edge(self, source: str, target: str, type: EdgeType, **attrs: Any) -> None:
        if self.g.has_edge(source, target, key=type):
            self.g.edges[source, target, type].update(attrs)
        else:
            self.g.add_edge(source, target, key=type, type=type, **attrs)

    def set_type(self, node_id: str, type: NodeType) -> None:
        self.g.nodes[node_id]["type"] = type

    # ─── access ──────────────────────────────────────────────────────────────

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.g

    def node(self, node_id: str) -> dict:
        return self.g.nodes[node_id]

    def type_of(self, node_id: str) -> NodeType | None:
        return self.g.nodes[node_id]["type"] if node_id in self.g else None

    def nodes(self, *types: NodeType) -> list[str]:
        return [n for n, d in self.g.nodes(data=True) if not types or d["type"] in types]

    def out_edges(self, node_id: str, *types: EdgeType) -> list[tuple[str, dict]]:
        return [(t, d) for _, t, d in self.g.out_edges(node_id, data=True) if not types or d["type"] in types]

    def in_edges(self, node_id: str, *types: EdgeType) -> list[tuple[str, dict]]:
        return [(s, d) for s, _, d in self.g.in_edges(node_id, data=True) if not types or d["type"] in types]

    def successors(self, node_id: str, *types: EdgeType) -> list[str]:
        return [t for t, _ in self.out_edges(node_id, *types)]

    def predecessors(self, node_id: str, *types: EdgeType) -> list[str]:
        return [s for s, _ in self.in_edges(node_id, *types)]

    def has_edge(self, source: str, target: str, type: EdgeType) -> bool:
        return self.g.has_edge(source, target, key=type)

    def stats(self) -> dict:
        return {
            "nodes": self.g.number_of_nodes(),
            "edges": self.g.number_of_edges(),
            "node_types": dict(Counter(d["type"].value for _, d in self.g.nodes(data=True))),
            "edge_types": dict(Counter(d["type"].value for *_, d in self.g.edges(data=True))),
        }

    # ─── serialization ───────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "nodes": [{"id": n, **{k: str(v) if k == "type" else v for k, v in d.items()}} for n, d in self.g.nodes(data=True)],
            "edges": [
                {"source": s, "target": t, **{k: str(v) if k == "type" else v for k, v in d.items()}}
                for s, t, d in self.g.edges(data=True)
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "KnowledgeGraph":
        kg = cls()
        for n in data["nodes"]:
            attrs = {k: v for k, v in n.items() if k not in ("id", "type", "name")}
            kg.add_node(n["id"], NodeType(n["type"]), n["name"], **attrs)
        for e in data["edges"]:
            attrs = {k: v for k, v in e.items() if k not in ("source", "target", "type")}
            kg.add_edge(e["source"], e["target"], EdgeType(e["type"]), **attrs)
        return kg
