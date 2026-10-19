# Streamline v1

## Status and ownership

`wellmanifest/streamline` HOMEs the **intra-repository** structure contract: how a code base is
cut into components, how wide a unit may grow, which direction dependencies may point, in what
order a violation is repaired, and how stages that exchange data are bounded.

It ADOPTs, and must not redefine:

| Concern | Owner | Used here as |
|---|---|---|
| Layer vocabulary and direction (`foundation < domain < application < interface < deployment`) | `wellmanifest/modularity` | `layer` of a component |
| Lexicographic tiers (`floor` cannot be outranked by lower tiers) | `wellmanifest/priority` | `tier` of a component |
| Typed, addressable processes, plans, receipts | `wellmanifest/poa` | what a `decision` stage hands over to |
| Declarative decisions instead of `if/elif` ladders | `wellmanifest/policy-dsl`, `wellmanifest/nohardcode` (NOHARDCODE-004) | data-driven selection |
| Search before generate, clone detection | `wellmanifest/reuse` | where extracted code goes (`packages/`) |
| Evidence-bound performance claims | `wellmanifest/performance` | proving that a split did not slow the system |

`modularity` composes independently owned modules *between* repositories and pins them by Git
revision. Streamline starts where it stops: it looks *inside* one repository, at import
structure, and says nothing about revisions, contracts or ownership across repositories.

The words MUST, MUST NOT, SHOULD and MAY are normative. A profile and a report describe and
validate; they grant no authority to edit, merge, publish or deploy.

## 1. Problem: horizontal complexity

Complexity grows in two directions. *Vertical* complexity is depth: layers and call chains.
*Horizontal* complexity is width: one unit that knows, exposes or does too many things at the
same level. Typical forms are a module of thousands of lines, a class with over a hundred
methods, a function with a thousand lines, a module that imports dozens of siblings, and
cross-layer imports that make everything depend on everything.

Width hides in three places a line count does not show:

1. **Deferred imports.** An import inside a function is still a dependency. It only moves the
   cost from import time to run time and from the reviewer's sight to the incident.
2. **Facade imports.** Importing a package `__init__` that re-exports half the repository makes
   every consumer depend on all of it.
3. **Upward imports.** A storage module importing a service, or a foundation module importing an
   interface, makes the order of repair undefined.

## 2. Component model

A profile declares `components`. Every Python module under `package` MUST be owned by exactly
one component (`SL-UNOWNED`, `SL-OVERLAP`). Ownership is by repository-relative glob in `paths`
minus `exclude`. An `exclude` resolves an overlap; it never makes a module ownerless.

A component has:

- `layer`: one of the five modularity layers;
- `tier`: `floor`, `core` or `edge`, most to least critical;
- `stage` (optional): `source`, `transform`, `decision` or `sink`, its role in a data stream;
- `budgets` (optional): overrides of the profile budgets.

### 2.1 Direction rules

For every dependency edge between two modules owned by different components, deferred edges
included, `TYPE_CHECKING`-only imports excluded:

- **Layer direction.** A component MAY depend on the same or a lower layer. Otherwise
  `SL-LAYER-DIRECTION`.
- **Tier direction.** A component MAY depend on the same or a more critical tier. A `floor`
  component depending on an `edge` component is `SL-TIER-INVERSION`. This is the dependency
  *priority* rule: what must not fail cannot rest on what may.

The two rules are independent; satisfying one never waives the other.

### 2.2 Cycles

- Import-time cycles between components are `SL-CYCLE`; between modules `SL-MODULE-CYCLE`.
- A cycle that exists only through deferred imports is `SL-CYCLE-DEFERRED`, a warning. It is
  evidence of a cycle the language is hiding, not a pass.

## 3. Horizontal-complexity budgets

Budgets are finite non-negative integers. A component budget overrides the profile budget of
the same name. A value above its budget is an error with the matching code.

| Budget | Code | Measured as |
|---|---|---|
| `maxModuleLines` | `SL-BUDGET-LINES` | physical lines of a module |
| `maxFanOut` | `SL-BUDGET-FANOUT` | distinct internal modules imported, deferred included |
| `maxImports` | `SL-BUDGET-IMPORTS` | import statements, including deferred and typing-only |
| `maxPublicSymbols` | `SL-BUDGET-SYMBOLS` | public top-level functions and classes |
| `maxMethodsPerClass` | `SL-BUDGET-METHODS` | methods of the widest class |
| `maxFunctionLines` | `SL-BUDGET-FUNCTION` | lines of the longest function or method |

Profiles SHOULD pick limits from measurement (`streamline measure`), not from taste, and SHOULD
tighten them over time rather than loosen them.

### 3.1 Exceptions

Existing debt is recorded, never silently tolerated. An exception names a `module`, a `rule`, a
`reason` and an `expires` ISO date. A budget exception carries an integer `limit`; it excuses a
value up to that limit only, so debt can shrink but not grow. An expired or malformed exception
is `SL-EXCEPTION-INVALID` and its violation is reported again.

## 4. Streams

Components that exchange data SHOULD do so through declared channels rather than direct calls.
A `stream` has one `producer`, one or more `consumers`, a positive integer `bound` and a
`backpressure` policy (`block`, `drop-oldest` or `spill`).

- An unbounded channel is not streaming: `bound` is required (`SL-STREAM-INVALID`).
- A component MUST NOT consume its own stream; producer and consumers MUST be declared
  components.
- Channels between components MUST NOT form a cycle (`SL-STREAM-CYCLE`).
- A producer that imports its consumer directly bypasses its own channel
  (`SL-STREAM-COUPLING`).

Stage roles describe intent: a `source` emits, a `transform` maps, a `decision` selects among
declared alternatives by an ordered, data-driven rule, a `sink` terminates. Streamline checks
that the channel graph is bounded, acyclic and decoupled; it does not execute streams.

## 5. Decision-ordered repair

Fixing everything at once is not a plan. `streamline plan` orders error findings by a
lexicographic key, so no later element can outrank an earlier one:

1. tier of the owning component, `floor` first;
2. layer, lowest first, so what others depend on is stabilised before its dependents;
3. size of the overshoot, largest first.

The ordering is deterministic, propose-only and derived from the profile and the tree alone. It
selects the next refactoring; it does not perform it.

### 5.1 Cross-project URI process delegation (`DOCK-URI-001`)

When a planned refactoring extracts a reusable procedure shared across multiple repositories or
languages, projects SHOULD delegate execution to a `dockuri` URI process rather than duplicating
helper scripts:

- **Library call**: `uri('proc://<package>/<component>/<action>/v1', payload)` (or streaming
  `uri_jsonl(...)`).
- **CLI + JSONL stream**: `uri 'proc://<package>/<component>/<action>/v1' '{"key":"val"}'` or
  piped JSONL lines on `stdin`.
- **Lifecycle management**: generate, modify, or relocate URI processes via
  `uri('proc://system/process/manage/v1', {'action': 'generate|modify|relocate', 'target_uri': ...})`.

`streamline plan` attaches a deterministic `uriDelegation` descriptor (`targetUri`, `manageUri`,
and polyglot `python` / `bash` invocation snippets) to each ranked refactoring item.

## 6. Conformance

A conforming checker MUST:

1. reject unknown profile fields and duplicate JSON keys (`SL-PROFILE-INVALID`);
2. read files only;
3. emit stable codes from the catalog in `src/streamline.py`;
4. exit 0 only when no error finding exists, 1 otherwise, 2 for unusable input;
5. keep `schemas/streamline-profile.schema.v1.json` and its validator in agreement on every
   shipped example (enforced by `tests/test_streamline.py`).

## 7. Non-goals

- It does not parse languages other than Python in v1. The profile and rules are
  language-neutral; the shipped measurer is not.
- It does not define cross-repository composition (`modularity`).
- It is not a style linter and does not measure cyclomatic complexity.
