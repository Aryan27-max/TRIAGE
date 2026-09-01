# SUMMARY — build log

For a human returning after a break. One section per stage, appended, never rewritten.
Decisions and gaps, not diffs. Read `CLAUDE.md` first for the invariants.

---

## Stage 1 — Skeleton and policy engine · complete

### What was built

| Module | Responsible for |
|---|---|
| `error_policy.json` | Moved from `research/` to the repo root. Runtime data, not reference. Unmodified. |
| `src/policy/engine.py` | Loads and validates the table; `resolve` / `list_entries` / `coverage_summary`. The eight action classes, the retrying set, the model-eligible set. |
| `src/api/errors.py` | Razorpay error envelope. `TriageAPIError.to_envelope()` plus subclasses for unknown code, invalid query param, and not found. |
| `src/api/schemas.py` | Pydantic wire shapes, separate from the engine's domain objects. |
| `src/api/routes_errors.py` | `GET /v1/errors`, `/{code}`, `/meta/actions`, `/meta/coverage`. Reads the engine off `app.state`. |
| `src/api/main.py` | `create_app()` factory, lifespan loading one engine, CORS, `/health`, the single exception handler. |
| `tests/` | `conftest.py`, `test_policy_coverage.py`, `test_api_errors.py`. |

### Decisions not specified in CLAUDE.md or research/

- **The repo had no `.git`.** Git was walking up and finding a stray repo at `C:\` owned
  by TrustedInstaller, which it refuses to operate on. Ran `git init` inside `TRIAGE/`
  so `git mv` would work. Files are staged; nothing has been committed.
- **Unknown code on `GET /v1/errors/{code}` returns 404 `NOT_FOUND_ERROR`**, not I-2's 400
  `BAD_REQUEST_ERROR`. I-2's 400 governs submitting a code as *input* to a decision
  endpoint; asking the read-only taxonomy for a row that does not exist is a different
  failure and should not share a shape with it. `BadErrorCodeError` (400,
  `unknown_error_code`) is declared in `errors.py` and is currently unused — Stage 2's
  `POST /v1/recovery/decide` is what raises it.
- **Query parameters are taken as `str` and validated by hand**, not typed as enums or
  `bool`. FastAPI's own coercion would return a 422 with a `detail` blob, breaking
  envelope consistency. This covers `recoverable` too, which the brief did not call out.
- **Collections use `{entity, count, items}`**, mirroring the Downtime API shape in
  research/05 §5.3.
- **`PolicyEngine.actions_catalogue()` was added** beyond the four named methods.
  `/meta/actions` needs the action descriptions that live in the JSON, and routes are not
  allowed to read the file themselves.
- **`list_entries` raises `ValueError` on a bad filter value** even though the route
  validates first. Two layers, so a direct caller in Stage 2 cannot silently get `[]`
  back from a typo.
- **Validation is exhaustive, not fail-fast.** `PolicyLoadError.problems` lists every
  problem found across all 110 rows, so fixing a broken table is one pass, not eleven.
- **I-4's "scheduling action" is read as the three retrying classes.** In the table only
  `RETRY_SCHEDULED` actually carries a positive `min_wait_hours`; the check permits all
  three, matching the wording of the Stage 1 gate.
- `hatchling` build backend with `packages = ["src"]`, so `uv sync` can install the
  project itself alongside its dependencies.

### Deviations from the brief

The 404 decision above is the only one. Everything in the build list is present as
specified.

### On `error_policy.json`

Left untouched, and it does not need editing. All 110 rows pass the full validation pass:
no duplicates, no `UNMAPPED`, every action one of the eight, every family in ABSX,
`recoverable` agrees with the action class in all 110 rows, no positive `min_wait_hours`
outside the retrying classes, no empty text fields. Counts match the brief exactly.

### Tests — 75 passing

They guarantee:

- The taxonomy is exactly what CLAUDE.md claims: 110 unique codes, all resolving, all
  eight actions present with the exact per-action counts, exactly 27 recoverable, and
  those 27 are precisely the three retrying classes. Counts are hard-coded in the test,
  not derived from the file under test.
- **I-2** — unknown codes raise, the exception carries `.code`, and the near misses
  `""`, `" "`, `INSUFFICIENT_FUNDS`, `insufficient-funds`, `insufficient_fund` all raise.
  If anyone later adds case folding or fuzzy matching, these fail.
- **I-4** — positive `min_wait_hours` appears only on retrying actions.
- **I-1** — `MODEL_ELIGIBLE_ACTIONS` is exactly `{RETRY_SCHEDULED, SWITCH_RAIL}`, is a
  strict subset of the retrying set, and no unrecoverable code is model-eligible.
- The validation pass actually bites: six tampered-table tests (bad action, `UNMAPPED`,
  `recoverable` disagreeing with its action, a wait on `AWAIT_STATUS`, a bad family, a
  duplicate code) each fail at load, and the app **refuses to boot** on a broken table.
  Tampered copies go to `tmp_path`; `error_policy.json` is never written to.
- The 404 and 400 bodies carry the full six-key envelope and do not share a code or a
  reason.

Verified against a real `uvicorn` process, not only `TestClient`: `/health` reports 110
codes, `/v1/errors/insufficient_funds` returns `RETRY_SCHEDULED` at 72h,
`/v1/errors/meta/coverage` reports 27 of 110, `/v1/errors/fake` returns 404, `/docs`
renders.

### Open questions carried into Stage 2

- **`RETRY_NOW` has no sub-hour representation.** `min_wait_hours` is an integer and is 0
  for all three `RETRY_NOW` codes. "Re-attempt within seconds" is not expressible in the
  table, so the Stage 2 scheduler needs its own floor for that class rather than reading
  one from policy.
- **The table is read once at startup and never reloaded.** Editing `error_policy.json`
  requires a restart. Fine for a prototype; worth stating in the README.
- **No auth.** research/05 specifies `Authorization: Bearer <key>`; the brief excluded it.
  Endpoints are open.
- **`BadErrorCodeError` is declared but unused** until `/v1/recovery/decide` exists.
- **`research/08-ui-spec.md` is not in CLAUDE.md's file map or index.** It postdates that
  section. Worth folding in before Stage 5.
