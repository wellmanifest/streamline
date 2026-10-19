"""Read-only Python import and source-size measurement for structure checks."""
from __future__ import annotations

import ast
import symtable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


@dataclass
class ModuleInfo:
    path: str
    dotted: str
    lines: int = 0
    import_statements: int = 0
    public_symbols: int = 0
    max_methods: int = 0
    max_function_lines: int = 0
    longest_function: str = ""
    import_time: set[str] = field(default_factory=set)
    deferred: set[str] = field(default_factory=set)

    @property
    def fan_out(self) -> int:
        return len(self.import_time | self.deferred)


def _dotted(rel: Path) -> str:
    parts = list(rel.with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _collect_files(root: Path, package: str) -> list[Path]:
    base = root / package
    if base.is_file():
        return [base]
    return sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)


def _resolve(name: str, known: dict[str, str]) -> str | None:
    """Longest dotted prefix of ``name`` that is a known module."""
    parts = name.split(".")
    while parts:
        candidate = ".".join(parts)
        if candidate in known:
            return known[candidate]
        parts.pop()
    return None


class _ImportVisitor(ast.NodeVisitor):
    def __init__(self, module: ModuleInfo, known: dict[str, str], source: str | None) -> None:
        self.module = module
        self.known = known
        self.depth = 0
        self.typing_only = 0
        self.is_package = module.path.endswith("__init__.py")
        try:
            table = symtable.symtable(source, module.path, "exec") if source is not None else None
        except SyntaxError:
            table = None  # incomplete source can still have measurable static imports
        self.scopes: list[tuple[Any, dict[str, str]]] = [(table, {})]
        self.used_scopes: set[Any] = set()
        self.loader_names: set[str] = set()
        self.comprehension_names: set[str] = set()
        self.future_annotations = False

    def visit_Module(self, node: ast.Module) -> None:
        self.future_annotations = any(isinstance(n, ast.ImportFrom) and n.module == "__future__" and any(a.name == "annotations" for a in n.names) for n in node.body)
        self.generic_visit(node)

    def _targets(self, node: ast.AST) -> Iterable[str]:
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = self.module.dotted.split(".") if self.module.dotted else []
                keep = len(parts) - node.level + (1 if self.is_package else 0)
                prefix = ".".join(parts[: max(keep, 0)])
                base = ".".join(x for x in (prefix, base) if x)
            # `from pkg import name` depends on submodule pkg.name when it exists, otherwise on
            # pkg itself (the name is re-exported by pkg/__init__.py).
            plain_names = False
            for alias in node.names:
                sub = f"{base}.{alias.name}" if base else alias.name
                if sub in self.known:
                    yield sub
                else:
                    plain_names = True
            if plain_names:
                yield base

    def _record_names(self, names: Iterable[str]) -> None:
        if not self.typing_only:
            bucket = self.module.deferred if self.depth else self.module.import_time
            for name in names:
                target = _resolve(name, self.known)
                if target and target != self.module.path:
                    bucket.add(target)

    def _record(self, node: ast.Import | ast.ImportFrom) -> None:
        self.module.import_statements += 1
        self._record_names(self._targets(node))
        if self.typing_only:
            return
        aliases = self.scopes[-1][1]
        for alias in node.names:
            name = alias.asname or alias.name.split(".")[0]
            aliases.pop(name, None)
            kind = self._loader_alias(node, alias)
            if kind:
                aliases[name] = kind
                self.loader_names.add(name)

    visit_Import = visit_ImportFrom = _record  # type: ignore[assignment]

    @staticmethod
    def _loader_alias(node: ast.Import | ast.ImportFrom, alias: ast.alias) -> str | None:
        if isinstance(node, ast.Import):
            return "module" if alias.name == "importlib" else None
        if node.level == 0 and node.module == "importlib" and alias.name == "import_module":
            return "loader"
        return None

    @staticmethod
    def _symbol(table: Any, name: str) -> Any:
        try:
            return table.lookup(name) if table is not None else None
        except KeyError:
            return None

    def _global_binding(self, name: str) -> str | None:
        table, aliases = self.scopes[0]
        symbol = self._symbol(table, name)
        return aliases.get(name) if symbol is not None and not symbol.is_assigned() else None

    def _visible_scopes(self) -> Iterable[tuple[Any, dict[str, str]]]:
        skip_classes = False
        for table, aliases in reversed(self.scopes):
            if table is None or not (skip_classes and table.get_type() == "class"):
                yield table, aliases
            if table is not None:
                skip_classes |= table.get_type() == "function"

    def _binding(self, name: str) -> str | None:
        """Use lexical symbols to reject parameters, assignments and class-only names."""
        if not self._loader_name(name):
            return None
        for table, aliases in self._visible_scopes():
            if table is None:
                return None
            symbol = self._symbol(table, name)
            if symbol is None:
                continue
            if symbol.is_parameter() or symbol.is_assigned():
                return None
            if symbol.is_global() and table != self.scopes[0][0]:
                return self._global_binding(name)
            if symbol.is_local():
                return aliases.get(name)
        return None

    def _loader_name(self, name: str) -> bool:
        return name in self.loader_names and name not in self.comprehension_names

    def _enter(self, node: ast.AST, name: str) -> None:
        table = self.scopes[-1][0]
        children = table.get_children() if table is not None else []
        child = next((c for c in children if c.get_name() == name and c.get_lineno() == node.lineno and c not in self.used_scopes), None)
        if child is not None:
            self.used_scopes.add(child)
        self.scopes.append((child, {}))

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        # Decorators, defaults and annotations are evaluated in the enclosing scope.
        for child in [*node.decorator_list, node.args, *([node.returns] if node.returns and not self.future_annotations else [])]:
            self.visit(child)
        self._enter(node, node.name)
        self.depth += 1
        for child in node.body:
            self.visit(child)
        self.depth -= 1
        self.scopes.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = _function  # type: ignore[assignment]

    def visit_arg(self, node: ast.arg) -> None:
        if not self.future_annotations:
            self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value:
            self.visit(node.value)
        table = self.scopes[-1][0]
        if not self.future_annotations and table is not None and table.get_type() in {"module", "class"}:
            self.visit(node.annotation)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for child in [*node.decorator_list, *node.bases, *node.keywords]:
            self.visit(child)
        self._enter(node, node.name)
        for child in node.body:
            self.visit(child)
        self.scopes.pop()

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        self._enter(node, "lambda")
        self.depth += 1
        self.visit(node.body)
        self.depth -= 1
        self.scopes.pop()

    def _comprehension(self, node: Any) -> None:
        self.visit(node.generators[0].iter)
        outer_names = self.comprehension_names
        self.comprehension_names = outer_names | {n.id for g in node.generators for n in ast.walk(g.target) if isinstance(n, ast.Name)}
        self._enter(node, {ast.ListComp: "listcomp", ast.SetComp: "setcomp", ast.DictComp: "dictcomp", ast.GeneratorExp: "genexpr"}[type(node)])
        if self.scopes[-1][0] is None:  # eager comprehensions are inlined on Python 3.12+
            self.scopes[-1] = self.scopes[-2]
        self.depth += isinstance(node, ast.GeneratorExp)
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)
            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)
        for child in ([node.key, node.value] if isinstance(node, ast.DictComp) else [node.elt]):
            self.visit(child)
        self.depth -= isinstance(node, ast.GeneratorExp)
        self.scopes.pop()
        self.comprehension_names = outer_names

    visit_ListComp = visit_SetComp = visit_DictComp = visit_GeneratorExp = _comprehension

    def visit_Call(self, node: ast.Call) -> None:
        function = node.func
        canonical = (isinstance(function, ast.Name) and self._binding(function.id) == "loader") or (
            isinstance(function, ast.Attribute) and function.attr == "import_module"
            and isinstance(function.value, ast.Name) and self._binding(function.value.id) == "module")
        if canonical:
            name = self._literal_module(node)
            if name:
                self._record_names([name])
        self.generic_visit(node)

    @staticmethod
    def _call_arguments(node: ast.Call) -> dict[str, ast.expr]:
        if len(node.args) > 2 or any(isinstance(arg, ast.Starred) for arg in node.args):
            return {}
        args = dict(zip(("name", "package"), node.args))
        for keyword in node.keywords:
            if keyword.arg not in {"name", "package"} or keyword.arg in args:
                return {}
            args[keyword.arg] = keyword.value
        return args

    @classmethod
    def _literal_module(cls, node: ast.Call) -> str | None:
        args = cls._call_arguments(node)
        name = args.get("name")
        if not isinstance(name, ast.Constant) or not isinstance(name.value, str):
            return None
        if not name.value.startswith("."):
            return name.value
        package = args.get("package")
        if not isinstance(package, ast.Constant) or not isinstance(package.value, str):
            return None
        return cls._relative_name(name.value, package.value)

    @staticmethod
    def _relative_name(name: str, package: str) -> str | None:
        if not package:
            return None
        level = len(name) - len(name.lstrip("."))
        parts = package.split(".")
        if level > len(parts):
            return None
        return ".".join([*parts[:len(parts) - level + 1], *filter(None, name[level:].split("."))])

    def visit_If(self, node: ast.If) -> None:
        test = ast.unparse(node.test)
        if test in {"TYPE_CHECKING", "typing.TYPE_CHECKING"}:
            self.typing_only += 1
            for child in node.body:
                self.visit(child)
            self.typing_only -= 1
            for child in node.orelse:
                self.visit(child)
        else:
            self.generic_visit(node)


def _measure_symbols(info: ModuleInfo, tree: ast.Module) -> bool:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and not node.name.startswith("_"):
            info.public_symbols += 1
        if isinstance(node, ast.ClassDef):
            methods = sum(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in node.body)
            info.max_methods = max(info.max_methods, methods)
    has_loader = False
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            has_loader |= any(_ImportVisitor._loader_alias(node, alias) is not None for alias in node.names)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            length = (node.end_lineno or node.lineno) - node.lineno + 1
            if length > info.max_function_lines:
                info.max_function_lines, info.longest_function = length, node.name
    return has_loader


def measure(root: Path, package: str) -> dict[str, ModuleInfo]:
    files = _collect_files(root, package)
    rels = {f: f.relative_to(root) for f in files}
    known = {_dotted(rel): rel.as_posix() for rel in rels.values()}
    modules: dict[str, ModuleInfo] = {}
    for file, rel in rels.items():
        text = file.read_text(encoding="utf-8", errors="replace")
        info = ModuleInfo(path=rel.as_posix(), dotted=_dotted(rel), lines=len(text.splitlines()))
        try:
            tree = ast.parse(text)
        except SyntaxError:
            modules[info.path] = info
            continue
        has_loader = _measure_symbols(info, tree)
        _ImportVisitor(info, known, text if has_loader else None).visit(tree)
        info.deferred -= info.import_time
        modules[info.path] = info
    return modules
