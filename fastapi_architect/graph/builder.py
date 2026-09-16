"""Assemble per-module facts into a typed KnowledgeGraph."""
import re
from pathlib import Path

from fastapi_architect.files import iter_python_files
from fastapi_architect.graph.extract import (
    DEPENDS_ON_ANNOTATION,
    ClassFacts,
    FunctionFacts,
    ModuleFacts,
    extract_module,
)
from fastapi_architect.graph.model import (
    CLASS_TYPES,
    FUNCTION_TYPES,
    MODEL_TYPES,
    EdgeType,
    KnowledgeGraph,
    NodeType,
)
from fastapi_architect.graph.resolve import Resolver

PYDANTIC_BASES = {"BaseModel", "BaseSettings", "RootModel", "GenericModel"}
ORM_DECLARATIVE_BASES = {"DeclarativeBase", "DeclarativeBaseNoMeta"}
_SCHEMA_SUFFIXES = re.compile(
    r"(Create|Update|Patch|Public|Private|Read|Out|In|InDB|DB|Base|Response|Request|Schema|Detail|Summary|List|Item)$"
)


def build_graph(project_root: str | Path) -> KnowledgeGraph:
    root = Path(project_root).resolve()
    modules = [extract_module(path, root) for path in sorted(iter_python_files(root))]
    return GraphBuilder(modules).build()


class GraphBuilder:
    def __init__(self, modules: list[ModuleFacts]) -> None:
        self.modules = modules
        self.resolver = Resolver(modules)
        self.kg = KnowledgeGraph()
        self._classes: dict[str, tuple[ModuleFacts, ClassFacts]] = {}
        self._aliases: dict[str, list[str]] = {}  # alias id → resolved dependency ids

    def build(self) -> KnowledgeGraph:
        self._declare_nodes()
        self._classify_classes()
        self._orm_edges()
        self._resolve_aliases()
        for m in self.modules:
            for fn in m.functions:
                self._function_edges(m, fn)
            self._module_edges(m)
        self._assign_function_roles()
        self._compute_full_paths()
        self._mirrors()
        return self.kg

    # ─── helpers ─────────────────────────────────────────────────────────────

    def _symbol(self, module: str, name: str) -> str | None:
        """Resolve to the id of an existing graph node."""
        ref = self.resolver.resolve(module, name)
        return ref.id if ref and ref.kind == "symbol" and ref.id in self.kg else None

    def _dependency(self, module: str, name: str) -> str:
        """Dependency target id; unknown callables (vars, externals) become Dependency nodes."""
        ref = self.resolver.resolve(module, name)
        if ref and ref.kind == "symbol":
            if ref.id not in self.kg:
                self.kg.add_node(ref.id, NodeType.DEPENDENCY, ref.qualname, module=ref.module)
            return ref.id
        node_id = f"external:{name}"
        if node_id not in self.kg:
            self.kg.add_node(node_id, NodeType.DEPENDENCY, name, external=True)
        return node_id

    def _table(self, name: str) -> str:
        node_id = f"table:{name}"
        if node_id not in self.kg:
            self.kg.add_node(node_id, NodeType.TABLE, name)
        return node_id

    def _add_query(self, source: str, sql: list[tuple[str, str]]) -> None:
        ops: dict[str, set[str]] = {}
        for table, op in sql:
            ops.setdefault(table, set()).add(op)
        for table, table_ops in ops.items():
            target = self._table(table)
            existing = set(self.kg.g.edges[source, target, EdgeType.QUERIES]["ops"]) if self.kg.has_edge(source, target, EdgeType.QUERIES) else set()
            self.kg.add_edge(source, target, EdgeType.QUERIES, ops=sorted(existing | table_ops))

    # ─── passes ──────────────────────────────────────────────────────────────

    def _declare_nodes(self) -> None:
        for m in self.modules:
            mod_id = self.kg.add_node(f"module:{m.module}", NodeType.MODULE, m.module, file=m.file, **({"error": m.error} if m.error else {}))
            for c in m.classes:
                node_id = f"{m.module}:{c.qualname}"
                self._classes[node_id] = (m, c)
                self.kg.add_node(node_id, NodeType.CLASS, c.qualname, module=m.module, file=m.file, line=c.line, fields=c.fields)
                self.kg.add_edge(self._parent(m, c.qualname), node_id, EdgeType.DEFINES)
            for fn in m.functions:
                node_id = f"{m.module}:{fn.qualname}"
                self.kg.add_node(node_id, NodeType.FUNCTION, fn.qualname, module=m.module, file=m.file, line=fn.line, is_async=fn.is_async)
                self.kg.add_edge(self._parent(m, fn.qualname), node_id, EdgeType.DEFINES)
            for v in m.variables:
                node_id = f"{m.module}:{v.name}"
                self.kg.add_node(node_id, NodeType(v.kind), v.name, module=m.module, file=m.file, line=v.line, prefix=v.prefix)
                self.kg.add_edge(mod_id, node_id, EdgeType.DEFINES)

    @staticmethod
    def _parent(m: ModuleFacts, qualname: str) -> str:
        return f"{m.module}:{qualname.rsplit('.', 1)[0]}" if "." in qualname else f"module:{m.module}"

    def _classify_classes(self) -> None:
        orm_base_vars = {f"{m.module}:{v}" for m in self.modules for v in m.orm_base_vars}
        resolved_bases: dict[str, tuple[list[str], set[str]]] = {}  # id → (project bases, external base names)
        for node_id, (m, c) in self._classes.items():
            project, external = [], set()
            for base in c.bases:
                ref = self.resolver.resolve(m.module, base)
                if ref and ref.kind == "symbol" and (ref.id in self._classes or ref.id in orm_base_vars):
                    project.append(ref.id)
                else:
                    external.add(base.rsplit(".", 1)[-1])
            resolved_bases[node_id] = (project, external)
            for base_id in project:
                if base_id in self._classes:
                    self.kg.add_edge(node_id, base_id, EdgeType.INHERITS)

        def ancestry(node_id: str, seen: set[str]) -> tuple[set[str], set[str]]:
            """All project ancestors and external base names."""
            if node_id in seen or node_id not in resolved_bases:
                return set(), set()
            seen.add(node_id)
            project, external = resolved_bases[node_id]
            ancestors, externals = set(project), set(external)
            for base_id in project:
                a, e = ancestry(base_id, seen)
                ancestors |= a
                externals |= e
            return ancestors, externals

        self._fields: dict[str, set[str]] = {}
        for node_id, (m, c) in self._classes.items():
            ancestors, externals = ancestry(node_id, set())
            fields = set(c.fields)
            for a in ancestors:
                if a in self._classes:
                    fields |= set(self._classes[a][1].fields)
            self._fields[node_id] = fields

            if "SQLModel" in externals:
                is_table = any(
                    self._classes[x][1].keywords.get("table") == "True" for x in [node_id, *ancestors] if x in self._classes
                )
                node_type = NodeType.ORM_MODEL if c.keywords.get("table") == "True" or (is_table and c.tablename) else NodeType.SCHEMA
            elif externals & PYDANTIC_BASES:
                node_type = NodeType.SCHEMA
            elif externals & ORM_DECLARATIVE_BASES or ancestors & orm_base_vars:
                is_base_itself = ORM_DECLARATIVE_BASES & set(resolved_bases[node_id][1]) and not c.tablename and not c.fields
                node_type = NodeType.CLASS if is_base_itself or c.abstract else NodeType.ORM_MODEL
            else:
                node_type = NodeType.CLASS
            self.kg.set_type(node_id, node_type)
            if node_type == NodeType.CLASS and (externals & ORM_DECLARATIVE_BASES or ancestors & orm_base_vars):
                self.kg.node(node_id)["orm_base"] = True

    def _orm_edges(self) -> None:
        for node_id, (m, c) in self._classes.items():
            if self.kg.type_of(node_id) != NodeType.ORM_MODEL:
                continue
            tablename = c.tablename or (c.qualname.rsplit(".", 1)[-1].lower() if c.keywords.get("table") == "True" else None)
            if tablename:
                self.kg.node(node_id)["tablename"] = tablename
                self.kg.add_edge(node_id, self._table(tablename.lower()), EdgeType.MAPS_TO)
            for table in c.foreign_tables:
                self.kg.add_edge(node_id, self._table(table), EdgeType.REFERENCES)
            for ref in c.relationship_refs:
                target = self.resolver.resolve_class_name(m.module, ref)
                if target and target != node_id and self.kg.type_of(target) == NodeType.ORM_MODEL:
                    self.kg.add_edge(node_id, target, EdgeType.RELATES_TO)

    def _resolve_aliases(self) -> None:
        for m in self.modules:
            for alias in m.aliases:
                self._aliases[f"{m.module}:{alias.name}"] = [
                    self._dependency(m.module, d) for d in alias.depends_refs if d != DEPENDS_ON_ANNOTATION
                ]

    def _function_edges(self, m: ModuleFacts, fn: FunctionFacts) -> None:
        fn_id = f"{m.module}:{fn.qualname}"

        for p in fn.params:
            is_dependency = bool(p.depends_refs)
            for dep in p.depends_refs:
                if dep == DEPENDS_ON_ANNOTATION:
                    if p.type_refs:
                        self.kg.add_edge(fn_id, self._dependency(m.module, p.type_refs[0]), EdgeType.DEPENDS_ON, via="param", param=p.name)
                else:
                    self.kg.add_edge(fn_id, self._dependency(m.module, dep), EdgeType.DEPENDS_ON, via="param", param=p.name)
            for t in p.type_refs:
                ref = self.resolver.resolve(m.module, t)
                if ref and ref.id in self._aliases:
                    is_dependency = True
                    for dep_id in self._aliases[ref.id]:
                        self.kg.add_edge(fn_id, dep_id, EdgeType.DEPENDS_ON, via="annotated", param=p.name)
            if not is_dependency:
                for t in p.type_refs:
                    if (target := self._symbol(m.module, t)) and self.kg.type_of(target) in MODEL_TYPES:
                        self.kg.add_edge(fn_id, target, EdgeType.ACCEPTS, param=p.name)
            if p.source:
                self.kg.node(fn_id).setdefault("param_sources", {})[p.name] = p.source

        for t in fn.return_refs:
            if (target := self._symbol(m.module, t)) and self.kg.type_of(target) in MODEL_TYPES:
                self.kg.add_edge(fn_id, target, EdgeType.RETURNS)

        for r in fn.routes:
            route_id = f"route:{r.method}:{fn_id}"
            self.kg.add_node(route_id, NodeType.ROUTE, f"{r.method} {r.path}", method=r.method, path=r.path,
                             module=m.module, file=m.file, line=r.line)
            self.kg.add_edge(route_id, fn_id, EdgeType.HANDLED_BY)
            if r.owner and (owner := self._symbol(m.module, r.owner)) and self.kg.type_of(owner) in (NodeType.APP, NodeType.ROUTER):
                self.kg.add_edge(owner, route_id, EdgeType.HAS_ROUTE)
            for dep in r.depends_refs:
                self.kg.add_edge(route_id, self._dependency(m.module, dep), EdgeType.DEPENDS_ON, via="decorator")
            for t in r.response_model_refs:
                if (target := self._symbol(m.module, t)) and self.kg.type_of(target) in MODEL_TYPES:
                    self.kg.add_edge(fn_id, target, EdgeType.RETURNS, via="response_model")

        for owner in fn.middleware_owners:
            if (owner_id := self._symbol(m.module, owner)) and self.kg.type_of(owner_id) == NodeType.APP:
                self.kg.add_edge(owner_id, fn_id, EdgeType.MIDDLEWARE)
                self.kg.node(fn_id)["middleware"] = True

        for call in fn.calls:
            target = self._symbol(m.module, call)
            if target is None or target == fn_id:
                continue
            node_type = self.kg.type_of(target)
            if node_type in FUNCTION_TYPES:
                self.kg.add_edge(fn_id, target, EdgeType.CALLS)
            elif node_type in CLASS_TYPES:
                self.kg.add_edge(fn_id, target, EdgeType.USES)
        for name in fn.names:
            if (target := self._symbol(m.module, name)) and self.kg.type_of(target) in CLASS_TYPES:
                self.kg.add_edge(fn_id, target, EdgeType.USES)

        self._add_query(fn_id, fn.sql)
        for template in fn.templates:
            template_id = f"template:{template}"
            self.kg.add_node(template_id, NodeType.TEMPLATE, template)
            self.kg.add_edge(fn_id, template_id, EdgeType.RENDERS)

    def _module_edges(self, m: ModuleFacts) -> None:
        self._add_query(f"module:{m.module}", m.sql)

        for v in m.variables:
            for dep in v.depends_refs:
                self.kg.add_edge(f"{m.module}:{v.name}", self._dependency(m.module, dep), EdgeType.DEPENDS_ON, via="router")

        for inc in m.includes:
            owner, router = self._symbol(m.module, inc.owner), self._symbol(m.module, inc.router)
            if not owner or not router or self.kg.type_of(router) != NodeType.ROUTER:
                continue
            self.kg.add_edge(owner, router, EdgeType.INCLUDES, prefix=inc.prefix)
            for dep in inc.depends_refs:
                self.kg.add_edge(router, self._dependency(m.module, dep), EdgeType.DEPENDS_ON, via="include_router")

        for mw in m.middlewares:
            owner = self._symbol(m.module, mw.owner)
            if not owner:
                continue
            target = self._symbol(m.module, mw.target)
            if target is None:
                target = f"external:{mw.target}"
                self.kg.add_node(target, NodeType.MIDDLEWARE, mw.target, external=True)
            self.kg.add_edge(owner, target, EdgeType.MIDDLEWARE)

    def _assign_function_roles(self) -> None:
        for node_id in self.kg.nodes(NodeType.FUNCTION):
            if self.kg.predecessors(node_id, EdgeType.HANDLED_BY):
                self.kg.set_type(node_id, NodeType.HANDLER)
            elif self.kg.predecessors(node_id, EdgeType.DEPENDS_ON):
                self.kg.set_type(node_id, NodeType.DEPENDENCY)
            elif self.kg.node(node_id).get("middleware"):
                self.kg.set_type(node_id, NodeType.MIDDLEWARE)

    def _compute_full_paths(self) -> None:
        def prefixes(owner: str, seen: frozenset[str]) -> list[tuple[str, bool]]:
            """(accumulated prefix, reachable from an App) for every include chain above `owner`."""
            own = self.kg.node(owner).get("prefix", "") if self.kg.type_of(owner) == NodeType.ROUTER else ""
            if self.kg.type_of(owner) == NodeType.APP:
                return [(own, True)]
            parents = [(s, d.get("prefix", "")) for s, d in self.kg.in_edges(owner, EdgeType.INCLUDES) if s not in seen]
            if not parents:
                return [(own, False)]
            return [(above + inc + own, mounted) for parent, inc in parents for above, mounted in prefixes(parent, seen | {owner})]

        for route_id in self.kg.nodes(NodeType.ROUTE):
            route = self.kg.node(route_id)
            owners = self.kg.predecessors(route_id, EdgeType.HAS_ROUTE)
            chains = [c for o in owners for c in prefixes(o, frozenset())] or [("", False)]
            full = sorted({_join_path(prefix, route["path"]) for prefix, _ in chains})
            route["full_paths"] = full
            route["mounted"] = any(mounted for _, mounted in chains)
            route["name"] = f"{route['method']} {full[0]}"

    def _mirrors(self) -> None:
        orm_by_name: dict[str, list[str]] = {}
        for node_id in self.kg.nodes(NodeType.ORM_MODEL):
            orm_by_name.setdefault(self.kg.node(node_id)["name"].rsplit(".", 1)[-1].lower(), []).append(node_id)

        for schema_id in self.kg.nodes(NodeType.SCHEMA):
            name = self.kg.node(schema_id)["name"].rsplit(".", 1)[-1]
            stem = name
            while (stripped := _SCHEMA_SUFFIXES.sub("", stem)) and stripped != stem:
                stem = stripped
            for orm_id in orm_by_name.get(stem.lower(), []):
                schema_fields, orm_fields = self._fields.get(schema_id, set()), self._fields.get(orm_id, set())
                if not schema_fields:
                    continue
                confidence = round(len(schema_fields & orm_fields) / len(schema_fields), 2)
                if confidence > 0:
                    self.kg.add_edge(schema_id, orm_id, EdgeType.MIRRORS, confidence=confidence)


def _join_path(*parts: str) -> str:
    joined = "/" + "/".join(p.strip("/") for p in parts if p.strip("/"))
    return joined + "/" if parts and parts[-1].endswith("/") and joined != "/" else joined
