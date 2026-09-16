"""Cross-module name resolution using each module's import table."""
from dataclasses import dataclass

from fastapi_architect.graph.extract import ModuleFacts


@dataclass(frozen=True)
class Ref:
    kind: str  # "module" | "symbol"
    module: str
    qualname: str = ""

    @property
    def id(self) -> str:
        return f"{self.module}:{self.qualname}" if self.kind == "symbol" else f"module:{self.module}"


class Resolver:
    def __init__(self, modules: list[ModuleFacts]) -> None:
        self.modules = {m.module: m for m in modules}
        self._qualnames = {
            m.module: {*m.symbols, *(f.qualname for f in m.functions), *(c.qualname for c in m.classes)}
            for m in modules
        }
        self._by_suffix: dict[str, list[str]] = {}
        for name in self.modules:
            parts = name.split(".")
            for i in range(1, len(parts)):
                self._by_suffix.setdefault(".".join(parts[i:]), []).append(name)
        self._class_names: dict[str, list[str]] = {}
        for m in modules:
            for c in m.classes:
                self._class_names.setdefault(c.qualname.rsplit(".", 1)[-1], []).append(f"{m.module}:{c.qualname}")

    def find_module(self, name: str) -> str | None:
        """Exact module name, or unique suffix match (project root above the import root)."""
        if name in self.modules:
            return name
        candidates = self._by_suffix.get(name, [])
        return candidates[0] if len(candidates) == 1 else None

    def resolve(self, module: str, name: str, _depth: int = 0) -> Ref | None:
        """Resolve a dotted name as seen from `module` to a project module or symbol."""
        if _depth > 10 or module not in self.modules:
            return None
        head, *rest = name.split(".")
        facts = self.modules[module]

        if head in self._qualnames[module]:
            ref: Ref | None = Ref("symbol", module, head)
        elif head in facts.imports:
            target, attr = facts.imports[head]
            ref = self._module_ref(target) if attr is None else self._member(self._module_ref(target), attr, _depth)
        else:
            return None

        for part in rest:
            ref = self._member(ref, part, _depth)
            if ref is None:
                return None
        return ref

    def resolve_class_name(self, module: str, name: str) -> str | None:
        """Resolve normally, else fall back to a project-unique class name (SQLAlchemy string refs)."""
        if (ref := self.resolve(module, name)) and ref.kind == "symbol":
            return ref.id
        candidates = self._class_names.get(name.rsplit(".", 1)[-1], [])
        return candidates[0] if len(candidates) == 1 else None

    def _module_ref(self, name: str) -> Ref | None:
        found = self.find_module(name)
        return Ref("module", found) if found else None

    def _member(self, ref: Ref | None, attr: str, depth: int) -> Ref | None:
        if ref is None:
            return None
        if ref.kind == "module":
            if sub := self.find_module(f"{ref.module}.{attr}"):
                return Ref("module", sub)
            if attr in self._qualnames[ref.module]:
                return Ref("symbol", ref.module, attr)
            if attr in self.modules[ref.module].imports:  # re-export, e.g. from package __init__
                return self.resolve(ref.module, attr, depth + 1)
            return None
        qualname = f"{ref.qualname}.{attr}"
        return Ref("symbol", ref.module, qualname) if qualname in self._qualnames[ref.module] else None
