# wellmanifest/streamline

Intra-repository structure standard: how a code base is cut into components, how wide a unit
may grow, which way dependencies may point, in which order violations are repaired, and how
stages that exchange data are bounded.

Status: `0.1.0-dev`, experimental. Normative text: [docs/STANDARD.md](docs/STANDARD.md).

## Why a new pack

Checked against `wellmanifest/*` on 2026-10-19: nothing covers this.

| Pack | Covers | Gap |
|---|---|---|
| `modularity` | composing modules *between* repositories, five ordered layers, DAG, pinned revisions | nothing inside one repository, no width budgets |
| `priority` | ranking work with lexicographic tiers | not applied to dependencies |
| `reuse` | search before generate, clone detection | no structure budget, no direction rules |
| `poa` | typed processes, plans, receipts | not a code-structure contract |
| `nohardcode` | forbids `if/elif` ladders, fixed constants | no budget for module or function width |

Streamline adopts the layer vocabulary from `modularity` and the tiers from `priority`, and
adds only what was missing: horizontal-complexity budgets, tier-ordered dependency direction,
deferred-import accounting, bounded stream channels and a decision-ordered repair plan.

## Use

```bash
python -m pip install -e .            # no runtime dependencies
streamline measure <root> --package <dir>      # import-structure metrics as JSON
streamline check   profile.json --root <root>  # exit 0 pass, 1 findings, 2 unusable input
streamline plan    profile.json --root <root>  # propose-only repair order
python -m pytest tests                          # 35 tests
```

A profile declares components (`paths`, `layer`, `tier`, `budgets`), optional `streams` and
expiring `exceptions`. See [examples/planfile.streamline.json](examples/planfile.streamline.json).

## First measurement (adopter example: planfile, 181 modules, 37.7k lines)

| Finding | Count |
|---|---|
| function over 120 lines (`server.py` has one of 1874 lines) | 13 |
| upward layer import | 10 |
| tier inversion (floor depends on edge/core) | 9 |
| module over 800 lines (`store.py` 3155, `server.py` 4228) | 8 |
| class over 40 methods (`Store`: 115) | 2 |
| import-time component cycle (`interfaces <-> services`, through `planfile/__init__.py`) | 2 |
| function-level imports hiding a dependency | 97 edges |

`streamline plan` ranks the repair: first the two foundation-to-higher-layer imports
(`project_paths.py -> sync/state.py`, `core/schema.py -> core/configuration.py`), then
`core/store.py`, and only then the edge-tier modules.

## Boundaries

- Reads files only; never edits a repository; grants no authority.
- v1 measures Python. The profile and the rules are language-neutral.
- Cross-repository composition stays in `modularity`.
