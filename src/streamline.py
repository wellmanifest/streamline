"""wellmanifest/streamline reference checker.

Dependency-free. Measures the import structure of a Python source tree and checks it against a
``wellmanifest.streamline/profile/v1`` document: ownership, layer direction, tier (priority)
direction, cycles, horizontal-complexity budgets, bounded stream channels, and expiring exceptions.

    python -m streamline measure <root> --package <dir>
    python -m streamline check   <profile.json> [--root <root>] [--format json]
    python -m streamline plan    <profile.json> [--root <root>]

``check`` exits 0 when no error finding exists, 1 otherwise, 2 for unusable input. The checker
only reads files; it never edits a repository and grants no authority.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Sequence

from streamline_measure import ModuleInfo, measure

SCHEMA = "wellmanifest.streamline/profile/v1"
# Layer vocabulary is owned by wellmanifest/modularity; tiers by wellmanifest/priority.
LAYERS = ("foundation", "domain", "application", "interface", "deployment")
TIERS = ("floor", "core", "edge")
BACKPRESSURE = ("block", "drop-oldest", "spill")
STAGES = ("source", "transform", "decision", "sink")
BUDGET_KEYS = {
    "maxModuleLines": "SL-BUDGET-LINES",
    "maxFanOut": "SL-BUDGET-FANOUT",
    "maxImports": "SL-BUDGET-IMPORTS",
    "maxPublicSymbols": "SL-BUDGET-SYMBOLS",
    "maxMethodsPerClass": "SL-BUDGET-METHODS",
    "maxFunctionLines": "SL-BUDGET-FUNCTION",
}

CATALOG: dict[str, str] = {
    "SL-PROFILE-INVALID": "The profile is malformed.",
    "SL-UNOWNED": "A module is not owned by any component.",
    "SL-OVERLAP": "A module is owned by more than one component.",
    "SL-LAYER-DIRECTION": "A component imports a component in a higher layer.",
    "SL-TIER-INVERSION": "A more critical tier depends on a less critical tier.",
    "SL-CYCLE": "Import-time dependency cycle between components.",
    "SL-MODULE-CYCLE": "Import-time dependency cycle between modules.",
    "SL-CYCLE-DEFERRED": "Cycle that exists only through deferred (function-level) imports.",
    "SL-BUDGET-LINES": "Module exceeds its line budget.",
    "SL-BUDGET-FANOUT": "Module depends on too many distinct internal modules.",
    "SL-BUDGET-IMPORTS": "Module has too many import statements.",
    "SL-BUDGET-SYMBOLS": "Module exposes too many public top-level symbols.",
    "SL-BUDGET-METHODS": "A class has too many methods.",
    "SL-BUDGET-FUNCTION": "A function is too long.",
    "SL-STREAM-INVALID": "A stream channel declaration is invalid.",
    "SL-STREAM-CYCLE": "Stream channels form a cycle between components.",
    "SL-STREAM-COUPLING": "A producer imports its consumer directly and bypasses the channel.",
    "SL-EXCEPTION-INVALID": "An exception is malformed or expired.",
}


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    subject: str
    message: str
    overshoot: int = 0  # how far a budget finding is over its limit; 0 for other findings

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "severity": self.severity, "subject": self.subject, "message": self.message}
        if self.overshoot:
            out["overshoot"] = self.overshoot
        return out


# --------------------------------------------------------------------------- measurement


def _sccs(graph: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's strongly connected components with more than one node, deterministic order."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on_stack: set[str] = set()
    out: list[list[str]] = []
    counter = [0]

    def visit(start: str) -> None:
        work = [(start, iter(sorted(graph.get(start, ()))))]
        index[start] = low[start] = counter[0]
        counter[0] += 1
        stack.append(start)
        on_stack.add(start)
        while work:
            node, children = work[-1]
            advanced = False
            for child in children:
                if child not in graph:
                    continue
                if child not in index:
                    index[child] = low[child] = counter[0]
                    counter[0] += 1
                    stack.append(child)
                    on_stack.add(child)
                    work.append((child, iter(sorted(graph.get(child, ())))))
                    advanced = True
                    break
                if child in on_stack:
                    low[node] = min(low[node], index[child])
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                members = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.append(member)
                    if member == node:
                        break
                if len(members) > 1:
                    out.append(sorted(members))

    for node in sorted(graph):
        if node not in index:
            visit(node)
    return sorted(out)


# --------------------------------------------------------------------------- profile


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_profile(profile: Any) -> list[Finding]:
    bad: list[Finding] = []

    def fail(subject: str, message: str) -> None:
        bad.append(Finding("SL-PROFILE-INVALID", "error", subject, message))

    if not isinstance(profile, dict):
        return [Finding("SL-PROFILE-INVALID", "error", "$", "profile must be an object")]
    if profile.get("schema") != SCHEMA:
        fail("schema", f"must equal {SCHEMA}")
    allowed = {"schema", "package", "budgets", "components", "streams", "exceptions", "note"}
    for key in sorted(set(profile) - allowed):
        fail(key, "unknown field")
    if not isinstance(profile.get("package"), str) or not profile.get("package"):
        fail("package", "must be a non-empty string")
    elif profile["package"].startswith("/") or ".." in Path(profile["package"]).parts:
        fail("package", "must be a repository-relative path")
    budgets = profile.get("budgets", {})
    if not isinstance(budgets, dict):
        fail("budgets", "must be an object")
        budgets = {}
    for key, value in budgets.items():
        if key not in BUDGET_KEYS:
            fail(f"budgets.{key}", "unknown budget")
        elif not _is_int(value) or value < 0:
            fail(f"budgets.{key}", "must be a non-negative integer")
    components = profile.get("components")
    seen: set[str] = set()
    if not isinstance(components, list) or not components:
        fail("components", "must be a non-empty array")
        components = []
    for i, comp in enumerate(components):
        where = f"components[{i}]"
        if not isinstance(comp, dict):
            fail(where, "must be an object")
            continue
        for key in sorted(set(comp) - {"id", "paths", "exclude", "layer", "tier", "stage", "budgets", "note"}):
            fail(f"{where}.{key}", "unknown field")
        cid = comp.get("id")
        if not isinstance(cid, str) or not cid:
            fail(f"{where}.id", "must be a non-empty string")
        elif cid in seen:
            fail(f"{where}.id", f"duplicate component id {cid}")
        else:
            seen.add(cid)
        paths = comp.get("paths")
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) and p for p in paths):
            fail(f"{where}.paths", "must be a non-empty array of glob strings")
        exclude = comp.get("exclude", [])
        if not isinstance(exclude, list) or not all(isinstance(p, str) and p for p in exclude):
            fail(f"{where}.exclude", "must be an array of glob strings")
        if comp.get("layer") not in LAYERS:
            fail(f"{where}.layer", f"must be one of {', '.join(LAYERS)}")
        if comp.get("tier") not in TIERS:
            fail(f"{where}.tier", f"must be one of {', '.join(TIERS)}")
        if comp.get("stage", "transform") not in STAGES:
            fail(f"{where}.stage", f"must be one of {', '.join(STAGES)}")
        for key, value in (comp.get("budgets") or {}).items():
            if key not in BUDGET_KEYS or not _is_int(value) or value < 0:
                fail(f"{where}.budgets.{key}", "unknown budget or negative value")
    for i, stream in enumerate(profile.get("streams", []) or []):
        where = f"streams[{i}]"
        if not isinstance(stream, dict):
            fail(where, "must be an object")
            continue
        for key in sorted(set(stream) - {"id", "producer", "consumers", "bound", "backpressure", "note"}):
            fail(f"{where}.{key}", "unknown field")
    for i, exc in enumerate(profile.get("exceptions", []) or []):
        if not isinstance(exc, dict):
            fail(f"exceptions[{i}]", "must be an object")
    return bad


def _owner_map(modules: dict[str, ModuleInfo], components: list[dict[str, Any]]) -> tuple[dict[str, str], list[Finding]]:
    owner: dict[str, str] = {}
    findings: list[Finding] = []
    for path in sorted(modules):
        owners = [
            c["id"]
            for c in components
            if any(fnmatch.fnmatchcase(path, g) for g in c["paths"])
            and not any(fnmatch.fnmatchcase(path, g) for g in c.get("exclude", []))
        ]
        if not owners:
            findings.append(Finding("SL-UNOWNED", "error", path, "no component owns this module"))
        elif len(owners) > 1:
            findings.append(Finding("SL-OVERLAP", "error", path, f"owned by {', '.join(owners)}"))
        else:
            owner[path] = owners[0]
    return owner, findings


def _exceptions(profile: dict[str, Any], today: date) -> tuple[dict[tuple[str, str], int], list[Finding]]:
    allowed: dict[tuple[str, str], int] = {}
    findings: list[Finding] = []
    for i, exc in enumerate(profile.get("exceptions", []) or []):
        subject = f"exceptions[{i}]"
        try:
            expires = date.fromisoformat(str(exc.get("expires")))
        except ValueError:
            findings.append(Finding("SL-EXCEPTION-INVALID", "error", subject, "expires must be an ISO date"))
            continue
        if exc.get("rule") not in BUDGET_KEYS.values() and exc.get("rule") not in {"SL-MODULE-CYCLE", "SL-LAYER-DIRECTION", "SL-TIER-INVERSION"}:
            findings.append(Finding("SL-EXCEPTION-INVALID", "error", subject, "unknown rule"))
        elif not exc.get("reason") or not isinstance(exc.get("module"), str):
            findings.append(Finding("SL-EXCEPTION-INVALID", "error", subject, "module and reason are required"))
        elif expires < today:
            findings.append(Finding("SL-EXCEPTION-INVALID", "error", subject, f"expired on {expires}"))
        elif exc["rule"] in BUDGET_KEYS.values() and not _is_int(exc.get("limit")):
            findings.append(Finding("SL-EXCEPTION-INVALID", "error", subject, "a budget exception needs an integer limit"))
        else:
            allowed[(exc["module"], exc["rule"])] = exc.get("limit", 0) if _is_int(exc.get("limit")) else 0
    return allowed, findings


def check(profile: Any, root: Path, today: date | None = None) -> list[Finding]:
    findings = validate_profile(profile)
    if findings:
        return findings
    today = today or date.today()
    components = profile["components"]
    modules = measure(root, profile["package"])
    owner, ownership = _owner_map(modules, components)
    findings += ownership
    allowed, exception_findings = _exceptions(profile, today)
    findings += exception_findings
    by_id = {c["id"]: c for c in components}
    layer_rank = {name: i for i, name in enumerate(LAYERS)}
    tier_rank = {name: i for i, name in enumerate(TIERS)}  # lower index = more critical

    def excused(path: str, rule: str, value: int | None = None) -> bool:
        key = (path, rule)
        if key not in allowed:
            return False
        return value is None or value <= allowed[key] or rule not in BUDGET_KEYS.values()

    # Direction rules over every edge, including deferred ones: a function-level import is still
    # a dependency, it only hides the cost.
    for path in sorted(owner):
        info = modules[path]
        src = by_id[owner[path]]
        for target in sorted(info.import_time | info.deferred):
            if target not in owner or owner[target] == owner[path]:
                continue
            dst = by_id[owner[target]]
            edge = f"{path} -> {target}"
            if layer_rank[src["layer"]] < layer_rank[dst["layer"]] and not excused(path, "SL-LAYER-DIRECTION"):
                findings.append(Finding("SL-LAYER-DIRECTION", "error", edge, f"{src['layer']} component {src['id']} imports {dst['layer']} component {dst['id']}"))
            if tier_rank[src["tier"]] < tier_rank[dst["tier"]] and not excused(path, "SL-TIER-INVERSION"):
                findings.append(Finding("SL-TIER-INVERSION", "error", edge, f"{src['tier']} component {src['id']} depends on {dst['tier']} component {dst['id']}"))

    import_time = {p: {t for t in modules[p].import_time if t in owner} for p in owner}
    everything = {p: {t for t in modules[p].import_time | modules[p].deferred if t in owner} for p in owner}
    comp_edges: dict[str, set[str]] = {c["id"]: set() for c in components}
    comp_all: dict[str, set[str]] = {c["id"]: set() for c in components}
    for p in owner:
        for t in import_time[p]:
            if owner[t] != owner[p]:
                comp_edges[owner[p]].add(owner[t])
        for t in everything[p]:
            if owner[t] != owner[p]:
                comp_all[owner[p]].add(owner[t])
    for cycle in _sccs(comp_edges):
        findings.append(Finding("SL-CYCLE", "error", " <-> ".join(cycle), "components import each other at import time"))
    for cycle in _sccs(import_time):
        if not all(excused(m, "SL-MODULE-CYCLE") for m in cycle):
            findings.append(Finding("SL-MODULE-CYCLE", "error", " <-> ".join(cycle), "modules import each other at import time"))
    hard = {tuple(c) for c in _sccs(comp_edges)}
    for cycle in _sccs(comp_all):
        if tuple(cycle) not in hard and not any(set(cycle) <= set(h) for h in hard):
            findings.append(Finding("SL-CYCLE-DEFERRED", "warning", " <-> ".join(cycle), "cycle exists only through function-level imports"))

    global_budgets = profile.get("budgets", {})
    for path in sorted(owner):
        info = modules[path]
        budgets = {**global_budgets, **(by_id[owner[path]].get("budgets") or {})}
        measured = {
            "maxModuleLines": (info.lines, f"{info.lines} lines"),
            "maxFanOut": (info.fan_out, f"{info.fan_out} internal dependencies"),
            "maxImports": (info.import_statements, f"{info.import_statements} import statements"),
            "maxPublicSymbols": (info.public_symbols, f"{info.public_symbols} public symbols"),
            "maxMethodsPerClass": (info.max_methods, f"{info.max_methods} methods in one class"),
            "maxFunctionLines": (info.max_function_lines, f"{info.longest_function or '?'} has {info.max_function_lines} lines"),
        }
        for key, limit in budgets.items():
            value, text = measured[key]
            rule = BUDGET_KEYS[key]
            if value > limit and not excused(path, rule, value):
                findings.append(Finding(rule, "error", path, f"{text} exceeds {limit}", overshoot=value - limit))

    findings += _check_streams(profile, by_id, comp_all)
    return sorted(findings, key=lambda f: (f.severity != "error", f.code, f.subject))


def _check_streams(profile: dict[str, Any], by_id: dict[str, Any], comp_all: dict[str, set[str]]) -> list[Finding]:
    findings: list[Finding] = []
    stream_edges: dict[str, set[str]] = {c: set() for c in by_id}
    seen: set[str] = set()
    for i, stream in enumerate(profile.get("streams", []) or []):
        sid = stream.get("id") if isinstance(stream.get("id"), str) else f"streams[{i}]"

        def bad(message: str, sid: str = sid) -> None:
            findings.append(Finding("SL-STREAM-INVALID", "error", sid, message))

        if sid in seen:
            bad("duplicate stream id")
        seen.add(sid)
        producer = stream.get("producer")
        consumers = stream.get("consumers")
        if producer not in by_id:
            bad("producer is not a declared component")
            continue
        if not isinstance(consumers, list) or not consumers or any(c not in by_id for c in consumers):
            bad("consumers must be a non-empty array of declared components")
            continue
        if producer in consumers:
            bad("a component cannot consume its own stream")
        if not _is_int(stream.get("bound")) or stream["bound"] < 1:
            bad("bound must be a positive integer: an unbounded channel is not streaming")
        if stream.get("backpressure") not in BACKPRESSURE:
            bad(f"backpressure must be one of {', '.join(BACKPRESSURE)}")
        for consumer in consumers:
            stream_edges[producer].add(consumer)
            if consumer in comp_all.get(producer, set()):
                findings.append(Finding("SL-STREAM-COUPLING", "error", f"{sid}: {producer} -> {consumer}", "producer imports its consumer directly instead of emitting to the channel"))
    for cycle in _sccs(stream_edges):
        findings.append(Finding("SL-STREAM-CYCLE", "error", " <-> ".join(cycle), "stream channels form a cycle"))
    return findings


# --------------------------------------------------------------------------- planning


def _uri_delegation(package: str, comp_id: str | None, module_path: str) -> dict[str, Any]:
    """Build deterministic DOCK-URI-001 delegation metadata for a planned refactoring item."""
    mod_slug = Path(module_path).stem.replace("_", "-") or "module"
    scope = (comp_id or "core").replace("_", "-")
    pkg = package.replace("_", "-")
    target_uri = f"proc://{pkg}/{scope}/{mod_slug}/v1"
    return {
        "targetUri": target_uri,
        "manageUri": "proc://system/process/manage/v1",
        "python": f"from dockuri import uri\nresult = uri({target_uri!r}, {{'action': 'run'}})",
        "cliJsonl": f"printf '%s\\n' '{{\"action\":\"run\"}}' | uri {target_uri!r}",
    }


def plan(profile: Any, root: Path, today: date | None = None) -> list[dict[str, Any]]:
    """Deterministic refactoring order for error findings.

    Lexicographic, so a lower key can never be outranked by a higher one: most critical tier
    first, then lowest layer first (stabilise what others depend on), then largest overshoot.
    """
    findings = [f for f in check(profile, root, today) if f.severity == "error"]
    if any(f.code == "SL-PROFILE-INVALID" for f in findings):
        return [f.as_dict() | {"rank": 0} for f in findings]
    modules = measure(root, profile["package"])
    owner, _ = _owner_map(modules, profile["components"])
    by_id = {c["id"]: c for c in profile["components"]}
    rows = []
    for f in findings:
        path = f.subject.split(" ")[0]
        comp = by_id.get(owner.get(path, ""))
        comp_id = comp["id"] if comp else None
        rows.append({
            **f.as_dict(),
            "component": comp_id,
            "key": [TIERS.index(comp["tier"]) if comp else len(TIERS), LAYERS.index(comp["layer"]) if comp else len(LAYERS), -f.overshoot],
            "uriDelegation": _uri_delegation(profile["package"], comp_id, path),
        })
    rows.sort(key=lambda r: (r["key"], r["code"], r["subject"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows


# --------------------------------------------------------------------------- CLI


def _load(path: Path) -> Any:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            if k in out:
                raise ValueError(f"duplicate key {k}")
            out[k] = v
        return out

    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="streamline", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    m = sub.add_parser("measure", help="print import-structure metrics as JSON")
    m.add_argument("root", type=Path)
    m.add_argument("--package", required=True)
    for name in ("check", "plan"):
        c = sub.add_parser(name)
        c.add_argument("profile", type=Path)
        c.add_argument("--root", type=Path, default=None, help="default: the profile's directory")
        c.add_argument("--format", choices=("text", "json"), default="text")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "measure":
            modules = measure(args.root.resolve(), args.package)
            print(json.dumps({p: {"lines": m.lines, "fanOut": m.fan_out, "imports": m.import_statements, "importTime": sorted(m.import_time), "deferred": sorted(m.deferred), "publicSymbols": m.public_symbols, "maxMethods": m.max_methods, "maxFunctionLines": m.max_function_lines} for p, m in modules.items()}, indent=2, sort_keys=True))
            return 0
        profile = _load(args.profile)
        root = (args.root or args.profile.parent).resolve()
        if args.command == "plan":
            rows = plan(profile, root)
            print(json.dumps(rows, indent=2) if args.format == "json" else "\n".join(f"{r['rank']:>3}. {r['code']} {r['subject']}: {r['message']}" for r in rows))
            return 1 if rows else 0
        findings = check(profile, root)
    except (OSError, ValueError) as exc:
        print(f"SL-INPUT-UNUSABLE: {exc}", file=sys.stderr)
        return 2
    errors = [f for f in findings if f.severity == "error"]
    if args.format == "json":
        print(json.dumps({"schema": "wellmanifest.streamline/report/v1", "ok": not errors, "findings": [f.as_dict() for f in findings]}, indent=2))
    else:
        for f in findings:
            print(f"{f.severity.upper():7} {f.code} {f.subject}: {f.message}")
        print(f"STREAMLINE-{'PASS' if not errors else 'FAIL'}: {len(errors)} errors, {len(findings) - len(errors)} warnings")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
