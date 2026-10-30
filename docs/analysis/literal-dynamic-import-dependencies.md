---
{
  "schema": "wellmanifest.docs/document/v1",
  "id": "literal-dynamic-import-dependencies",
  "kind": "analysis",
  "version": 1,
  "title": "Literal dynamic imports remain architecture dependencies",
  "status": "proposed",
  "owner": "wellmanifest/streamline",
  "created": "2026-10-20",
  "updated": "2026-10-20",
  "review_after": "2026-11-20",
  "source_revision": "e69790a201cb174b6c6d1cf568bdd95f19c05566",
  "affected_repositories": ["wellmanifest/streamline"],
  "evidence": [
    "https://github.com/wellmanifest/streamline/issues/1",
    "repo://wellmanifest/streamline/tests/test_dynamic_imports.py",
    "repo://wellmanifest/streamline/src/streamline_measure.py"
  ]
}
---

# Literal dynamic imports remain architecture dependencies

<!-- docs:section question -->
## Question

Can a literal loader call hide an upward dependency from structure checks?
Yes: the base revision visits import statements but ignores loader calls.

<!-- docs:section scope -->
## Scope

Python import measurement, existing direction/cycle/fan-out rules and wheel packaging.
No scanned application is executed or modified. Findings and plans grant no write authority.

<!-- docs:section method -->
## Method

Add failing synthetic regressions before implementation, then run `python3 -m pytest -q tests`.
Use Python's symbol tables for lexical binding and AST literals for module addresses.
Keep the existing `streamline.measure` and `ModuleInfo` exports; place measurement in
`streamline_measure` so rule evaluation and source analysis have separate responsibilities.

<!-- docs:section evidence -->
## Evidence

The full candidate suite passes 86 tests on Python 3.13. Against the base revision, 25 tests
fail and 61 pass with the same new regression cases. Tests cover absolute and relative names,
module/function aliases, lexical shadows, class closures, defaults, typing-only blocks,
annotations, eager/generator comprehensions, cycles, direction rules and source that raises
if executed. The existing schema/validator agreement tests remain part of the suite.
The built wheel imports both modules in a fresh process and detects the synthetic edge
without executing its source. Wheel SHA-256:
`8f3a006aab58dccc0b98ddc9b89a37f251536e876ce1399fe55eb00a9b65b554`.

<!-- docs:section facts -->
## Facts

- `importlib.import_module("app.api")` now contributes an internal edge, including deferred use.
- Loader calls affect fan-out and cycles; the import-statement counter retains its meaning.
- Computed addresses and missing relative packages are not guessed.
- Python 3.12+ inlines eager comprehension scopes. Explicit target-name masking preserves
  lexical shadows while retaining real loader edges; generator bodies remain deferred.
- Only modules containing a canonical loader import need the additional symbol-table parse.
- Both runtime modules are declared in `pyproject.toml`; distributing just `streamline.py`
  is insufficient after this change. The supported installation is the complete package.

<!-- docs:section hypotheses -->
## Hypotheses

More complete edges can increase the number of reported errors in previously passing trees.
That indicates newly visible debt; it does not prove the application recently regressed.

<!-- docs:section limitations -->
## Limitations

This is bounded syntax analysis, not whole-program import resolution. Wildcard imports,
indirect loader assignments, computed strings, control-flow feasibility and runtime
monkey-patching remain outside coverage. Other supported Python versions require their own
runtime qualification; this session tested 3.13. Existing complexity debt in rule evaluation
remains. Local tests and publication configuration do not prove protected CI, merge or deployment.

<!-- docs:section recommendations -->
## Recommendations

Publish through the independent Validator and rerun adopter audits against that exact revision
before accepting a clean architecture claim. Fix newly exposed upward dependencies at their
composition boundary; do not move them behind loader strings. Revert the ticket commit to
roll back measurement changes. Keep raw logs and generated wheels outside repository history.
