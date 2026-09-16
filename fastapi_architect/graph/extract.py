"""Per-module AST extraction.

Produces plain, JSON-serializable facts about a single file. Names are kept as raw dotted
references ("crud.get_users") and only resolved across modules by the builder, so facts can be
cached per file.
"""
import ast
import re
from dataclasses import dataclass, field
from pathlib import Path

HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
DEPENDS_CALLS = {"Depends", "Security"}
PARAM_SOURCES = {"Header", "Query", "Body", "Path", "Cookie", "Form", "File"}
TEMPLATE_CALLS = {"TemplateResponse", "get_template"}
TEMPLATE_EXTENSIONS = (".html", ".htm", ".jinja", ".jinja2", ".j2", ".xml", ".txt")
ORM_BASE_FACTORIES = {"declarative_base", "generate_base"}

# "Depends()" without argument: the dependency is the parameter's own annotation
DEPENDS_ON_ANNOTATION = "@annotation"

_SQL_START = re.compile(r"^\s*\(?\s*(SELECT|INSERT|UPDATE|DELETE|WITH|CREATE|ALTER|DROP|TRUNCATE)\b", re.I)
_SQL_TABLE = re.compile(
    r"\b(FROM|JOIN|INTO|UPDATE|TABLE(?:\s+IF\s+(?:NOT\s+)?EXISTS)?)\s+(\"[^\"]+\"|[A-Za-z_][\w.]*)(\s*\()?",
    re.I,
)
_SQL_CTE = re.compile(r"(?:\bWITH\s+(?:RECURSIVE\s+)?|,\s*)([A-Za-z_]\w*)\s+AS\s*(?:NOT\s+)?(?:MATERIALIZED\s+)?\(", re.I)
_SQL_KEYWORDS = {"select", "set", "values", "lateral", "only", "exists", "not", "if", "and", "or", "on", "where", "as", "the"}
_SQL_NOISE = re.compile(r"'(?:[^']|'')*'|--[^\n]*|/\*.*?\*/", re.S)  # string literals and comments


@dataclass
class ParamFacts:
    name: str
    type_refs: list[str] = field(default_factory=list)
    depends_refs: list[str] = field(default_factory=list)
    source: str | None = None  # Header, Query, Body...


@dataclass
class RouteFacts:
    owner: str | None
    method: str
    path: str
    line: int
    response_model_refs: list[str] = field(default_factory=list)
    depends_refs: list[str] = field(default_factory=list)


@dataclass
class FunctionFacts:
    qualname: str
    line: int
    is_async: bool
    params: list[ParamFacts] = field(default_factory=list)
    return_refs: list[str] = field(default_factory=list)
    routes: list[RouteFacts] = field(default_factory=list)
    middleware_owners: list[str] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    names: list[str] = field(default_factory=list)
    sql: list[tuple[str, str]] = field(default_factory=list)  # (table, op)
    templates: list[str] = field(default_factory=list)


@dataclass
class ClassFacts:
    qualname: str
    line: int
    bases: list[str] = field(default_factory=list)
    keywords: dict[str, str] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    tablename: str | None = None
    abstract: bool = False
    relationship_refs: list[str] = field(default_factory=list)
    foreign_tables: list[str] = field(default_factory=list)


@dataclass
class AliasFacts:
    name: str
    line: int
    depends_refs: list[str] = field(default_factory=list)


@dataclass
class VariableFacts:
    name: str
    kind: str  # "App" | "Router"
    line: int
    prefix: str = ""
    depends_refs: list[str] = field(default_factory=list)


@dataclass
class IncludeFacts:
    owner: str
    router: str
    line: int
    prefix: str = ""
    depends_refs: list[str] = field(default_factory=list)


@dataclass
class MiddlewareFacts:
    owner: str
    target: str
    line: int


@dataclass
class ModuleFacts:
    module: str
    file: str
    is_package: bool
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)
    symbols: list[str] = field(default_factory=list)
    functions: list[FunctionFacts] = field(default_factory=list)
    classes: list[ClassFacts] = field(default_factory=list)
    aliases: list[AliasFacts] = field(default_factory=list)
    variables: list[VariableFacts] = field(default_factory=list)
    includes: list[IncludeFacts] = field(default_factory=list)
    middlewares: list[MiddlewareFacts] = field(default_factory=list)
    orm_base_vars: list[str] = field(default_factory=list)
    sql: list[tuple[str, str]] = field(default_factory=list)
    error: str | None = None


# ─── helpers ──────────────────────────────────────────────────────────────────

def module_name(path: Path, root: Path) -> tuple[str, bool]:
    parts = list(path.relative_to(root).with_suffix("").parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts) or root.name, is_package


def dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _last(node: ast.AST) -> str | None:
    d = dotted(node)
    return d.rsplit(".", 1)[-1] if d else None


def _is_depends(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and _last(node.func) in DEPENDS_CALLS


def _depends_target(call: ast.Call) -> str | None:
    target = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "dependency"), None)
    if target is None:
        return DEPENDS_ON_ANNOTATION
    return dotted(target)


def _depends_list(node: ast.AST | None) -> list[str]:
    """Depends(...) targets inside a `dependencies=[...]` list."""
    if not isinstance(node, (ast.List, ast.Tuple)):
        return []
    return [t for elt in node.elts if _is_depends(elt) and (t := _depends_target(elt)) and t != DEPENDS_ON_ANNOTATION]


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _kw(call: ast.Call, name: str) -> ast.AST | None:
    return next((k.value for k in call.keywords if k.arg == name), None)


def type_refs(node: ast.AST | None) -> list[str]:
    """Dotted names referenced by a type expression (forward-ref strings included, Depends skipped)."""
    if node is None:
        return []
    if isinstance(node, (ast.Name, ast.Attribute)) and (d := dotted(node)):
        return [d]
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return type_refs(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return []
    if _is_depends(node):
        return []
    refs: list[str] = []
    for child in ast.iter_child_nodes(node):
        refs.extend(type_refs(child))
    return refs


def _string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "?" for v in node.values)
    return None


def sql_tables(text: str) -> list[tuple[str, str]]:
    """Tables referenced by a raw SQL string, with 'read' / 'write' / 'ddl' operation."""
    start = _SQL_START.match(text)
    if not start or (not start.group(1).isupper() and not re.search(r"[*=();%]", text)):
        return []  # lowercase prose like "select the best one from the list"
    text = _SQL_NOISE.sub("''", text)
    ctes = {m.group(1).lower() for m in _SQL_CTE.finditer(text)}
    found: dict[str, str] = {}
    for m in _SQL_TABLE.finditer(text):
        keyword, name, paren = m.group(1).upper(), m.group(2).strip('"').lower(), m.group(3)
        if paren and keyword in ("FROM", "JOIN"):
            continue  # function call: unnest(...), now()
        if name in ctes or name in _SQL_KEYWORDS or "?" in name:
            continue
        op = "ddl" if keyword.startswith("TABLE") else "write" if keyword in ("INTO", "UPDATE") else "read"
        if keyword == "FROM" and re.search(r"\bDELETE\s+$", text[: m.start()], re.I):
            op = "write"
        if found.get(name) not in ("write", "ddl"):
            found[name] = op
    return sorted(found.items())


# ─── extraction ───────────────────────────────────────────────────────────────

class _Extractor:
    def __init__(self, facts: ModuleFacts, tree: ast.Module) -> None:
        self.f = facts
        self.tree = tree

    def run(self) -> None:
        self._docstrings = {
            id(n.body[0].value)
            for n in ast.walk(self.tree)
            if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and n.body and isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant)
        }
        self._imports()
        for node in self.tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.f.symbols.append(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                self.f.symbols.extend(t.id for t in targets if isinstance(t, ast.Name))
                self._assignment(node)
            elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
                self.f.symbols.append(node.name.id)
                self._alias(node.name.id, node.value, node.lineno)
        self._definitions(self.tree.body, prefix="")
        self._module_calls()
        for node in self.tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                for sub in ast.walk(node):
                    if id(sub) not in self._docstrings and (text := _string_value(sub)) is not None:
                        self.f.sql.extend(sql_tables(text))

    def _imports(self) -> None:
        package = self.f.module.split(".") if self.f.is_package else self.f.module.split(".")[:-1]
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Import):
                for a in node.names:
                    if a.asname:
                        self.f.imports[a.asname] = (a.name, None)
                    else:
                        head = a.name.split(".")[0]
                        self.f.imports[head] = (head, None)
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    base = package[: len(package) - (node.level - 1)] if node.level > 1 else package
                    mod = ".".join([*base, node.module] if node.module else base)
                else:
                    mod = node.module or ""
                for a in node.names:
                    if a.name != "*":
                        self.f.imports[a.asname or a.name] = (mod, a.name)

    def _assignment(self, node: ast.Assign | ast.AnnAssign) -> None:
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = [t.id for t in targets if isinstance(t, ast.Name)]
        value = node.value
        if not names or value is None:
            return
        if isinstance(value, ast.Subscript) and _last(value.value) == "Annotated":
            for name in names:
                self._alias(name, value, node.lineno)
        elif isinstance(value, ast.Call):
            callee = _last(value.func)
            if callee in ("FastAPI", "APIRouter"):
                for name in names:
                    self.f.variables.append(VariableFacts(
                        name=name,
                        kind="App" if callee == "FastAPI" else "Router",
                        line=node.lineno,
                        prefix=_str(_kw(value, "prefix")) or _str(_kw(value, "root_path")) or "",
                        depends_refs=_depends_list(_kw(value, "dependencies")),
                    ))
            elif callee in ORM_BASE_FACTORIES:
                self.f.orm_base_vars.extend(names)

    def _alias(self, name: str, value: ast.AST, line: int) -> None:
        if isinstance(value, ast.Subscript) and _last(value.value) == "Annotated" and isinstance(value.slice, ast.Tuple):
            deps = [t for e in value.slice.elts[1:] if _is_depends(e) and (t := _depends_target(e))]
            if deps:
                self.f.aliases.append(AliasFacts(name=name, line=line, depends_refs=deps))

    def _definitions(self, body: list[ast.stmt], prefix: str) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self.f.functions.append(self._function(node, prefix + node.name))
            elif isinstance(node, ast.ClassDef):
                self.f.classes.append(self._class(node, prefix + node.name))
                self._definitions(node.body, prefix + node.name + ".")

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef, qualname: str) -> FunctionFacts:
        fn = FunctionFacts(qualname=qualname, line=node.lineno, is_async=isinstance(node, ast.AsyncFunctionDef))

        args = node.args
        positional = args.posonlyargs + args.args
        defaults: list[ast.AST | None] = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
        for arg, default in [*zip(positional, defaults), *zip(args.kwonlyargs, args.kw_defaults)]:
            if arg.arg in ("self", "cls"):
                continue
            fn.params.append(self._param(arg, default))

        fn.return_refs = type_refs(node.returns)

        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                continue
            owner, attr = dotted(dec.func.value), dec.func.attr
            if attr in HTTP_METHODS or attr in ("api_route", "websocket"):
                path = _str(dec.args[0]) if dec.args else _str(_kw(dec, "path"))
                if path is None:
                    continue
                if attr == "api_route":
                    methods_node = _kw(dec, "methods")
                    methods = [s.upper() for e in getattr(methods_node, "elts", []) if (s := _str(e))] or ["GET"]
                else:
                    methods = ["WS" if attr == "websocket" else attr.upper()]
                for method in methods:
                    fn.routes.append(RouteFacts(
                        owner=owner,
                        method=method,
                        path=path,
                        line=dec.lineno,
                        response_model_refs=type_refs(_kw(dec, "response_model")),
                        depends_refs=_depends_list(_kw(dec, "dependencies")),
                    ))
            elif attr == "middleware" and owner:
                fn.middleware_owners.append(owner)

        calls, names, templates = set(), set(), []
        for stmt in node.body:
            for sub in ast.walk(stmt):
                if isinstance(sub, ast.Call):
                    if (target := dotted(sub.func)) and not _is_depends(sub):
                        calls.add(target)
                    if _last(sub.func) in TEMPLATE_CALLS:
                        for a in [*sub.args, *(k.value for k in sub.keywords if k.arg in ("name", "template_name"))]:
                            if (s := _str(a)) and s.lower().endswith(TEMPLATE_EXTENSIONS):
                                templates.append(s)
                                break
                elif isinstance(sub, (ast.Name, ast.Attribute)) and isinstance(sub.ctx, ast.Load):
                    if d := dotted(sub):
                        names.add(d)
                elif id(sub) not in self._docstrings and (text := _string_value(sub)) is not None:
                    fn.sql.extend(sql_tables(text))
        fn.calls, fn.names, fn.templates = sorted(calls), sorted(names - calls), templates
        return fn

    def _param(self, arg: ast.arg, default: ast.AST | None) -> ParamFacts:
        p = ParamFacts(name=arg.arg)
        ann = arg.annotation
        if isinstance(ann, ast.Subscript) and _last(ann.value) == "Annotated" and isinstance(ann.slice, ast.Tuple):
            p.type_refs = type_refs(ann.slice.elts[0])
            for meta in ann.slice.elts[1:]:
                if _is_depends(meta):
                    p.depends_refs.append(_depends_target(meta) or DEPENDS_ON_ANNOTATION)
                elif isinstance(meta, ast.Call) and _last(meta.func) in PARAM_SOURCES:
                    p.source = _last(meta.func)
        else:
            p.type_refs = type_refs(ann)
        if _is_depends(default):
            p.depends_refs.append(_depends_target(default) or DEPENDS_ON_ANNOTATION)
        elif isinstance(default, ast.Call) and _last(default.func) in PARAM_SOURCES:
            p.source = _last(default.func)
        return p

    def _class(self, node: ast.ClassDef, qualname: str) -> ClassFacts:
        cls = ClassFacts(
            qualname=qualname,
            line=node.lineno,
            bases=[d for b in node.bases if (d := dotted(b))],
            keywords={k.arg: ast.unparse(k.value) for k in node.keywords if k.arg},
        )
        for item in node.body:
            target, value, annotation = None, None, None
            if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                target, value, annotation = item.target.id, item.value, item.annotation
            elif isinstance(item, ast.Assign) and len(item.targets) == 1 and isinstance(item.targets[0], ast.Name):
                target, value = item.targets[0].id, item.value
            if target is None:
                continue

            if target == "__tablename__":
                cls.tablename = _str(value)
            elif target == "__abstract__":
                cls.abstract = isinstance(value, ast.Constant) and value.value is True
            elif not target.startswith("__") and (annotation is not None or isinstance(value, ast.Call)):
                if isinstance(value, ast.Call) and _last(value.func) == "relationship":
                    ref = value.args[0] if value.args else None
                    refs = type_refs(ref) if ref is not None else type_refs(annotation)
                    cls.relationship_refs.extend(r for r in refs if r not in ("Mapped", "list", "List", "Optional", "set"))
                    cls.fields.append(target)
                    continue
                if annotation is not None or (isinstance(value, ast.Call) and _last(value.func) in ("Column", "mapped_column", "Field")):
                    cls.fields.append(target)

        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and _last(sub.func) == "ForeignKey" and sub.args:
                if (s := _str(sub.args[0])) and "." in s:
                    cls.foreign_tables.append(s.rsplit(".", 1)[0].lower())
        return cls

    def _module_calls(self) -> None:
        """include_router / add_middleware calls anywhere in the module (incl. app factories)."""
        for node in ast.walk(self.tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                continue
            owner = dotted(node.func.value)
            if owner is None or not node.args:
                continue
            target = dotted(node.args[0])
            if node.func.attr == "include_router" and target:
                self.f.includes.append(IncludeFacts(
                    owner=owner,
                    router=target,
                    line=node.lineno,
                    prefix=_str(_kw(node, "prefix")) or "",
                    depends_refs=_depends_list(_kw(node, "dependencies")),
                ))
            elif node.func.attr == "add_middleware" and target:
                self.f.middlewares.append(MiddlewareFacts(owner=owner, target=target, line=node.lineno))


def extract_module(path: Path, root: Path, source: str | None = None) -> ModuleFacts:
    module, is_package = module_name(path, root)
    facts = ModuleFacts(module=module, file=path.relative_to(root).as_posix(), is_package=is_package)
    if source is None:
        source = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        facts.error = f"SyntaxError: {e.msg} (line {e.lineno})"
        return facts
    _Extractor(facts, tree).run()
    return facts
