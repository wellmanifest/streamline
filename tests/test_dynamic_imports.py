"""Architecture checks must see literal loader calls without executing source."""
from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import streamline as sl  # noqa: E402


def measure(tmp_path, source, module="app/base.py"):
    for name in ("app/__init__.py", "app/base.py", "app/api.py", "app/sub/__init__.py", "app/sub/leaf.py"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")
    (tmp_path / module).write_text(textwrap.dedent(source))
    return sl.measure(tmp_path, "app")[module]


@pytest.mark.parametrize("source", [
    'import importlib\nimportlib.import_module("app.api")',
    'import importlib as loader\nloader.import_module("app.api")',
    'from importlib import import_module\nimport_module("app.api")',
    'from importlib import import_module as load\nload(name="app.api")',
    'import importlib\nimportlib.import_module(name="app.api", package=None)',
])
def test_absolute_literal_loaders_are_internal_dependencies(tmp_path, source):
    info = measure(tmp_path, source)
    assert info.import_time == {"app/api.py"}
    assert info.deferred == set()
    assert info.import_statements == 1  # a loader call is not an import statement


@pytest.mark.parametrize("call", [
    'load(".leaf", "app.sub")',
    'load("..api", package="app.sub")',
    'load(name=".leaf", package="app.sub")',
])
def test_relative_loader_names_use_the_explicit_literal_package(tmp_path, call):
    info = measure(tmp_path, 'from importlib import import_module as load\n' + call)
    expected = "app/api.py" if "..api" in call else "app/sub/leaf.py"
    assert info.import_time == {expected}


@pytest.mark.parametrize("source", [
    'def f():\n    import importlib as loader\n    return loader.import_module("app.api")',
    'from importlib import import_module as load\ndef f():\n    return load("app.api")',
    'import importlib\nasync def f():\n    return importlib.import_module("app.api")',
    'from importlib import import_module as load\nf = lambda: load("app.api")',
])
def test_function_and_lambda_loaders_remain_deferred(tmp_path, source):
    info = measure(tmp_path, source)
    assert info.import_time == set()
    assert info.deferred == {"app/api.py"}


@pytest.mark.parametrize("source", [
    'import importlib\ndef f(importlib): return importlib.import_module("app.api")',
    'from importlib import import_module as load\ndef f():\n    load = custom\n    return load("app.api")',
    'from importlib import import_module as load\ndef f():\n    load("app.api")\n    load = custom',
    'from importlib import import_module as load\nload = custom\nload("app.api")',
    'def import_module(name): pass\nimport_module("app.api")',
    'from another_library import import_module\nimport_module("app.api")',
    'from .importlib import import_module\nimport_module("app.api")',
    'import importlib\nf = lambda importlib: importlib.import_module("app.api")',
    'import importlib\n[importlib.import_module("app.api") for importlib in loaders]',
    'from importlib import import_module as load\ndef f():\n    try: pass\n    except Exception as load: pass\n    load("app.api")',
    'from importlib import import_module as load\ndef f(x):\n    match x:\n        case {"load": load}: pass\n    load("app.api")',
])
def test_shadowed_names_do_not_invent_a_dependency(tmp_path, source):
    info = measure(tmp_path, source)
    assert "app/api.py" not in info.import_time | info.deferred


def test_class_namespace_is_not_a_method_closure(tmp_path):
    info = measure(tmp_path, '''
        from importlib import import_module as load
        class C:
            load = custom
            def f(self):
                return load("app.api")
    ''')
    assert info.deferred == {"app/api.py"}


def test_nested_method_closes_over_function_and_skips_class_names(tmp_path):
    info = measure(tmp_path, '''
        def outer():
            from importlib import import_module as load
            class C:
                load = custom
                def f(self):
                    return load("app.api")
    ''')
    assert info.deferred == {"app/api.py"}


def test_local_loader_does_not_leak_into_sibling_function(tmp_path):
    info = measure(tmp_path, '''
        def f():
            from importlib import import_module as load
            return load("app.sub.leaf")
        def g():
            return load("app.api")
    ''')
    assert info.deferred == {"app/sub/leaf.py"}


def test_unknown_global_calls_do_not_crash_measurement(tmp_path):
    info = measure(tmp_path, '''
        def f():
            return unknown_global(len([]))
    ''')
    assert info.import_time | info.deferred == set()


def test_deferred_comprehension_and_global_loader_binding(tmp_path):
    info = measure(tmp_path, '''
        from importlib import import_module as load
        def f():
            global load
            return load("app.api")
        items = (load("app.sub.leaf") for _ in values)
    ''')
    assert info.import_time == set()
    assert info.deferred == {"app/api.py", "app/sub/leaf.py"}


@pytest.mark.parametrize("expression", [
    '[load("app.api") for _ in values]',
    '{load("app.api") for _ in values}',
    '{key: load("app.api") for key in values}',
])
def test_eager_comprehensions_keep_runtime_loader_edges(tmp_path, expression):
    info = measure(tmp_path, 'from importlib import import_module as load\n' + expression)
    assert info.import_time == {"app/api.py"}
    assert info.deferred == set()


def test_function_defaults_and_class_bodies_run_at_import_time(tmp_path):
    info = measure(tmp_path, '''
        from importlib import import_module as load
        def f(default=load("app.api")):
            pass
        class C:
            module = load("app.sub.leaf")
    ''')
    assert info.import_time == {"app/api.py", "app/sub/leaf.py"}
    assert info.deferred == set()


def test_type_checking_loaders_are_excluded_and_else_remains_runtime(tmp_path):
    info = measure(tmp_path, '''
        from typing import TYPE_CHECKING
        from importlib import import_module as load
        if TYPE_CHECKING:
            load("app.api")
        else:
            load("app.sub.leaf")
    ''')
    assert info.import_time == {"app/sub/leaf.py"}


@pytest.mark.parametrize("call", [
    'load(variable)', 'load("app." + name)', 'load(*args)',
    'load(**kwargs)', 'load(".api")', 'load(".api", dynamic_package)',
    'load("...api", "app")', 'load("app.api", unknown=True)',
])
def test_unresolved_or_invalid_calls_are_not_guessed(tmp_path, call):
    info = measure(tmp_path, 'from importlib import import_module as load\n' + call)
    assert info.import_time | info.deferred == set()


def test_loader_dependencies_enforce_direction_cycles_and_fanout(tmp_path):
    measure(tmp_path, 'import importlib\nimportlib.import_module("app.api")')
    (tmp_path / "app/api.py").write_text('from app import base\n')
    profile = {"schema": sl.SCHEMA, "package": "app", "components": [
        {"id": "foundation", "paths": ["app/base.py", "app/sub/*.py"], "layer": "foundation", "tier": "floor"},
        {"id": "interface", "paths": ["app/api.py", "app/__init__.py"], "layer": "interface", "tier": "edge"},
    ], "budgets": {"maxFanOut": 0}}
    findings = sl.check(profile, tmp_path)
    assert {"SL-LAYER-DIRECTION", "SL-TIER-INVERSION", "SL-CYCLE", "SL-MODULE-CYCLE", "SL-BUDGET-FANOUT"} <= {f.code for f in findings}
    (tmp_path / "app/base.py").write_text('import importlib\ndef late(): return importlib.import_module("app.api")\n')
    findings = sl.check(profile, tmp_path)
    assert "SL-CYCLE-DEFERRED" in {f.code for f in findings}
    assert not {"SL-CYCLE", "SL-MODULE-CYCLE"} & {f.code for f in findings}


def test_scanned_modules_are_never_executed(tmp_path):
    info = measure(tmp_path, 'import importlib\nraise RuntimeError("do not execute")\nimportlib.import_module("app.api")')
    assert info.import_time == {"app/api.py"}


@pytest.mark.parametrize("prefix,expected", [
    ("", {"app/api.py"}),
    ("from __future__ import annotations\n", set()),
])
def test_postponed_annotations_are_not_runtime_loaders(tmp_path, prefix, expected):
    info = measure(tmp_path, prefix + '''from importlib import import_module as load
def f(arg: load("app.api")) -> load("app.api"):
    pass
item: load("app.api")
''')
    assert info.import_time == expected
    assert info.deferred == set()


def test_local_variable_annotations_are_not_evaluated(tmp_path):
    info = measure(tmp_path, '''
        from importlib import import_module as load
        def f():
            local: load("app.api")
            value: load("app.api") = load("app.sub.leaf")
    ''')
    assert info.import_time == set()
    assert info.deferred == {"app/sub/leaf.py"}
