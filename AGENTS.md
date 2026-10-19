# AGENTS.md — wellmanifest/streamline

HOME wellmanifest, shape domain_pack.
ADOPT wellmanifest/{modularity, priority, poa, policy-dsl, nohardcode, reuse, performance, dsl, new-project}.

## Rules

1. This pack is **abstract**. No product or repository name appears in `docs/STANDARD.md`,
   `schemas/**` or `src/streamline.py`. Adopter profiles live in `examples/` only.
2. Do not redefine what another pack owns: layer vocabulary belongs to `modularity`, tiers to
   `priority`, processes to `poa`. Cite and use them.
3. The checker only reads files. `plan` is propose-only. Neither grants authority.
4. Every finding code in `src/streamline.py` `CATALOG` is documented in `docs/STANDARD.md`.
   A rule nothing tests is a defect: add the test in the same change.
5. `schemas/streamline-profile.schema.v1.json` and `validate_profile` must agree; the test suite
   asserts it on the shipped example and on mutations.
6. A daemon or CI service that runs the checker is a `runtime_service` and does not home here.
