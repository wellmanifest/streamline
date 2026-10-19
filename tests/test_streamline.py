from __future__ import annotations

import json
import sys
import textwrap
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import streamline as sl  # noqa: E402

TODAY = date(2026, 10, 19)


def write(root: Path, files: dict[str, str]) -> None:
    for name, body in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")


def profile(**overrides):
    base = {
        "schema": sl.SCHEMA,
        "package": "app",
        "components": [
            {"id": "base", "paths": ["app/base/*.py"], "layer": "foundation", "tier": "floor"},
            {"id": "domain", "paths": ["app/domain/*.py"], "layer": "domain", "tier": "core"},
            {"id": "api", "paths": ["app/api/*.py", "app/__init__.py"], "layer": "interface", "tier": "edge"},
        ],
    }
    base.update(overrides)
    return base


@pytest.fixture
def tree(tmp_path):
    write(tmp_path, {
        "app/__init__.py": "",
        "app/base/__init__.py": "",
        "app/base/util.py": "def helper(): return 1\n",
        "app/domain/__init__.py": "",
        "app/domain/model.py": "from app.base.util import helper\n\n\ndef build(): return helper()\n",
        "app/api/__init__.py": "",
        "app/api/routes.py": "from app.domain import model\nfrom app.base import util\n",
    })
    return tmp_path


def codes(findings, severity=None):
    return sorted(f.code for f in findings if severity in (None, f.severity))


def test_clean_tree_passes(tree):
    assert sl.check(profile(), tree, TODAY) == []


def test_measure_resolves_absolute_relative_and_from_imports(tmp_path):
    write(tmp_path, {
        "app/__init__.py": "",
        "app/a.py": "from . import b\nfrom .c import thing\nimport app.d\n",
        "app/b.py": "", "app/c.py": "thing = 1\n", "app/d.py": "",
    })
    info = sl.measure(tmp_path, "app")["app/a.py"]
    assert info.import_time == {"app/b.py", "app/c.py", "app/d.py"}
    assert info.import_statements == 3


def test_deferred_and_type_checking_imports_are_classified(tmp_path):
    write(tmp_path, {
        "app/__init__.py": "",
        "app/a.py": """
            from typing import TYPE_CHECKING
            if TYPE_CHECKING:
                from app.b import B
            def late():
                from app.c import C
            """,
        "app/b.py": "", "app/c.py": "",
    })
    info = sl.measure(tmp_path, "app")["app/a.py"]
    assert info.import_time == set()
    assert info.deferred == {"app/c.py"}  # TYPE_CHECKING imports are not runtime dependencies


def test_unowned_and_overlapping_modules(tree):
    p = profile()
    p["components"][2]["paths"] = ["app/api/*.py"]
    p["components"].append({"id": "dup", "paths": ["app/base/util.py"], "layer": "foundation", "tier": "floor"})
    assert codes(sl.check(p, tree, TODAY)) == ["SL-OVERLAP", "SL-UNOWNED"]


def test_exclude_resolves_an_overlap_without_hiding_it(tree):
    p = profile()
    p["components"].append({"id": "dup", "paths": ["app/base/*.py"], "layer": "foundation", "tier": "floor"})
    assert "SL-OVERLAP" in codes(sl.check(p, tree, TODAY))
    p["components"][3]["exclude"] = ["app/base/util.py"]
    p["components"][0]["paths"] = ["app/base/util.py"]
    assert sl.check(p, tree, TODAY) == []
    p["components"][3]["exclude"] = "app/base/util.py"
    assert codes(sl.check(p, tree, TODAY)) == ["SL-PROFILE-INVALID"]


def test_layer_direction_violation(tree):
    write(tree, {"app/base/util.py": "from app.api import routes\n"})
    found = sl.check(profile(), tree, TODAY)
    assert "SL-LAYER-DIRECTION" in codes(found)
    assert "SL-TIER-INVERSION" in codes(found)  # floor depending on edge as well


def test_tier_inversion_is_independent_of_layers(tree):
    p = profile()
    p["components"][0]["layer"] = "deployment"  # layer rule satisfied, tier rule must still fail
    write(tree, {"app/base/util.py": "from app.domain import model\n", "app/domain/model.py": "", "app/api/routes.py": "from app.domain import model\n"})
    assert codes(sl.check(p, tree, TODAY), "error") == ["SL-TIER-INVERSION"]


def test_deferred_import_still_counts_for_direction(tree):
    write(tree, {"app/base/util.py": "def f():\n    from app.api import routes\n"})
    assert "SL-LAYER-DIRECTION" in codes(sl.check(profile(), tree, TODAY))


def test_import_time_cycle_is_an_error_and_deferred_only_cycle_is_a_warning(tree):
    p = profile()
    p["components"][0]["layer"] = "domain"
    p["components"][1]["tier"] = "floor"
    write(tree, {"app/base/util.py": "from app.domain import model\n"})
    write(tree, {"app/domain/model.py": "from app.base import util\n"})
    found = sl.check(p, tree, TODAY)
    assert "SL-CYCLE" in codes(found, "error") and "SL-MODULE-CYCLE" in codes(found, "error")

    write(tree, {"app/domain/model.py": "def f():\n    from app.base import util\n"})
    found = sl.check(p, tree, TODAY)
    assert codes(found, "error") == []
    assert codes(found, "warning") == ["SL-CYCLE-DEFERRED"]


@pytest.mark.parametrize("budget,body,rule", [
    ("maxModuleLines", "x = 1\n" * 10, "SL-BUDGET-LINES"),
    ("maxFanOut", "import app.base.util\nimport app.domain.model\nimport app.domain\n", "SL-BUDGET-FANOUT"),
    ("maxImports", "import os\nimport sys\nimport json\n", "SL-BUDGET-IMPORTS"),
    ("maxPublicSymbols", "def a(): pass\ndef b(): pass\ndef c(): pass\n", "SL-BUDGET-SYMBOLS"),
    ("maxMethodsPerClass", "class C:\n    def a(self): pass\n    def b(self): pass\n    def c(self): pass\n", "SL-BUDGET-METHODS"),
    ("maxFunctionLines", "def long():\n    x = 1\n    y = 2\n    z = 3\n    return x\n", "SL-BUDGET-FUNCTION"),
])
def test_horizontal_complexity_budgets(tree, budget, body, rule):
    write(tree, {"app/api/routes.py": body})
    p = profile(budgets={budget: 2})
    assert rule in codes(sl.check(p, tree, TODAY), "error")
    p = profile(budgets={budget: 1000})
    assert rule not in codes(sl.check(p, tree, TODAY), "error")


def test_component_budget_overrides_global_budget(tree):
    write(tree, {"app/api/routes.py": "x = 1\n" * 10})
    p = profile(budgets={"maxModuleLines": 5})
    p["components"][2]["budgets"] = {"maxModuleLines": 50}
    assert codes(sl.check(p, tree, TODAY), "error") == []


def exception(**kw):
    return {"module": "app/api/routes.py", "rule": "SL-BUDGET-LINES", "limit": 20, "reason": "legacy", "expires": "2027-01-01", **kw}


def test_exception_excuses_only_up_to_its_limit_and_before_expiry(tree):
    write(tree, {"app/api/routes.py": "x = 1\n" * 10})
    base = {"budgets": {"maxModuleLines": 5}}
    assert codes(sl.check(profile(exceptions=[exception()], **base), tree, TODAY), "error") == []
    assert codes(sl.check(profile(exceptions=[exception(limit=8)], **base), tree, TODAY), "error") == ["SL-BUDGET-LINES"]
    expired = sl.check(profile(exceptions=[exception(expires="2026-01-01")], **base), tree, TODAY)
    assert "SL-EXCEPTION-INVALID" in codes(expired) and "SL-BUDGET-LINES" in codes(expired)
    assert "SL-EXCEPTION-INVALID" in codes(sl.check(profile(exceptions=[exception(reason="")], **base), tree, TODAY))
    assert "SL-EXCEPTION-INVALID" in codes(sl.check(profile(exceptions=[exception(limit=None)], **base), tree, TODAY))


STREAM = {"id": "events", "producer": "api", "consumers": ["domain"], "bound": 100, "backpressure": "block"}


def test_valid_stream_between_decoupled_components(tree):
    write(tree, {"app/api/routes.py": ""})
    assert sl.check(profile(streams=[STREAM]), tree, TODAY) == []


@pytest.mark.parametrize("mutation,expected", [
    ({"bound": 0}, "SL-STREAM-INVALID"),
    ({"bound": "many"}, "SL-STREAM-INVALID"),
    ({"backpressure": "ignore"}, "SL-STREAM-INVALID"),
    ({"producer": "ghost"}, "SL-STREAM-INVALID"),
    ({"consumers": []}, "SL-STREAM-INVALID"),
    ({"consumers": ["api"]}, "SL-STREAM-INVALID"),
])
def test_invalid_streams(tree, mutation, expected):
    write(tree, {"app/api/routes.py": ""})
    assert expected in codes(sl.check(profile(streams=[{**STREAM, **mutation}]), tree, TODAY))


def test_stream_cycle_and_coupling(tree):
    reverse = {"id": "back", "producer": "domain", "consumers": ["api"], "bound": 10, "backpressure": "spill"}
    write(tree, {"app/api/routes.py": ""})
    assert "SL-STREAM-CYCLE" in codes(sl.check(profile(streams=[STREAM, reverse]), tree, TODAY))
    write(tree, {"app/api/routes.py": "from app.domain import model\n"})
    assert "SL-STREAM-COUPLING" in codes(sl.check(profile(streams=[STREAM]), tree, TODAY))


@pytest.mark.parametrize("broken", [
    {"schema": "other"}, {"package": "../x"}, {"extra": 1}, {"budgets": {"maxLines": 1}},
    {"budgets": {"maxFanOut": -1}}, {"components": []},
])
def test_invalid_profiles_fail_closed(tree, broken):
    assert codes(sl.check(profile(**broken), tree, TODAY)) == ["SL-PROFILE-INVALID"] * len(sl.validate_profile(profile(**broken)))


def test_plan_orders_by_tier_then_layer_then_overshoot(tree):
    write(tree, {
        "app/base/util.py": "x = 1\n" * 30,
        "app/domain/model.py": "x = 1\n" * 90,
        "app/api/routes.py": "x = 1\n" * 500,
    })
    rows = sl.plan(profile(budgets={"maxModuleLines": 10}), tree, TODAY)
    assert [r["subject"] for r in rows] == ["app/base/util.py", "app/domain/model.py", "app/api/routes.py"]
    assert [r["rank"] for r in rows] == [1, 2, 3]  # floor outranks a far larger edge overshoot


def test_cli_exit_codes_and_json_report(tree, tmp_path, capsys):
    path = tree / "p.json"
    path.write_text(json.dumps(profile()))
    assert sl.main(["check", str(path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    path.write_text(json.dumps(profile(budgets={"maxModuleLines": 0})))
    assert sl.main(["check", str(path)]) == 1
    assert "STREAMLINE-FAIL" in capsys.readouterr().out
    path.write_text('{"schema": "x", "schema": "y"}')
    assert sl.main(["check", str(path)]) == 2
    assert sl.main(["measure", str(tree), "--package", "app"]) == 0
    assert "app/domain/model.py" in capsys.readouterr().out
    path.write_text(json.dumps(profile(budgets={"maxModuleLines": 0})))
    assert sl.main(["plan", str(path)]) == 1


def test_finding_codes_are_cataloged():
    used = {c for c in sl.CATALOG}
    assert set(sl.BUDGET_KEYS.values()) <= used
    for code in ("SL-CYCLE", "SL-LAYER-DIRECTION", "SL-TIER-INVERSION", "SL-STREAM-COUPLING"):
        assert code in used


ROOT = Path(__file__).resolve().parents[1]


def test_schema_and_validator_agree_on_shipped_example_and_mutations():
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((ROOT / "schemas/streamline-profile.schema.v1.json").read_text())
    validator = jsonschema.Draft202012Validator(schema)
    example = json.loads((ROOT / "examples/planfile.streamline.json").read_text())
    assert not list(validator.iter_errors(example))
    assert sl.validate_profile(example) == []
    mutations = [
        {"schema": "x"}, {"extra": 1}, {"package": "/abs"}, {"package": "../up"}, {"components": []},
        {"budgets": {"maxFanOut": -1}}, {"budgets": {"unknown": 1}},
    ]
    for mutation in mutations:
        broken = {**example, **mutation}
        assert list(validator.iter_errors(broken)), mutation
        assert sl.validate_profile(broken), mutation
    for field, value in (("layer", "middle"), ("tier", "high"), ("stage", "pump")):
        broken = json.loads(json.dumps(example))
        broken["components"][0][field] = value
        assert list(validator.iter_errors(broken)) and sl.validate_profile(broken), field
    stream = {"id": "s", "producer": "foundation", "consumers": ["storage"], "bound": 5, "backpressure": "block"}
    for field, value in (("bound", 0), ("backpressure", "x"), ("consumers", [])):
        broken = {**example, "streams": [{**stream, field: value}]}
        assert list(validator.iter_errors(broken)), field
        assert any(f.code == "SL-STREAM-INVALID" for f in sl.check(broken, ROOT, TODAY)), field


def test_unparsable_module_is_measured_not_fatal(tree):
    write(tree, {"app/api/broken.py": "def (:\n", "app/api/routes.py": ""})
    found = sl.check(profile(budgets={"maxModuleLines": 0}), tree, TODAY)
    assert any(f.subject == "app/api/broken.py" and f.code == "SL-BUDGET-LINES" for f in found)


def test_aliased_and_dotted_imports_resolve_to_the_longest_known_module(tmp_path):
    write(tmp_path, {
        "app/__init__.py": "", "app/pkg/__init__.py": "", "app/pkg/mod.py": "",
        "app/a.py": "import app.pkg.mod as m\nimport app.pkg.mod.deep.attr\nfrom app.pkg import mod, missing\n",
    })
    info = sl.measure(tmp_path, "app")["app/a.py"]
    assert info.import_time == {"app/pkg/mod.py", "app/pkg/__init__.py"}


def test_relative_imports_from_a_package_init_and_parent_levels(tmp_path):
    write(tmp_path, {
        "app/__init__.py": "", "app/x.py": "", "app/sub/__init__.py": "from . import leaf\nfrom .. import x\n",
        "app/sub/leaf.py": "from ..x import value\n",
    })
    modules = sl.measure(tmp_path, "app")
    assert modules["app/sub/__init__.py"].import_time == {"app/sub/leaf.py", "app/x.py"}
    assert modules["app/sub/leaf.py"].import_time == {"app/x.py"}


def test_plan_reports_overshoot_as_data_not_parsed_text(tree):
    write(tree, {"app/api/routes.py": "x = 1\n" * 40})
    (row,) = sl.plan(profile(budgets={"maxModuleLines": 10}), tree, TODAY)
    assert row["overshoot"] == 30 and row["key"][2] == -30


def test_checker_passes_its_own_profile():
    own = {"schema": sl.SCHEMA, "package": "src", "components": [
        {"id": "checker", "paths": ["src/streamline.py"], "layer": "foundation", "tier": "floor"}]}
    assert [f for f in sl.check(own, ROOT, TODAY) if f.code != "SL-BUDGET-LINES"] == []
