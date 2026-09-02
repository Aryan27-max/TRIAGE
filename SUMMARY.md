# SUMMARY — build log

For a human returning after a break. One section per stage, appended, never rewritten.
Decisions and gaps, not diffs. Read `CLAUDE.md` first for the invariants.

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
  `POST /v1/recovery/decide` is what raises it. *(Stage 2: it does, and I-2's wording in
  CLAUDE.md has been tightened to say "in the decision path".)*
- **Query parameters are taken as `str` and validated by hand**, not typed as enums or
  `bool` — FastAPI's coercion returns a 422 `detail` blob, breaking envelope consistency.
  Covers `recoverable` too, which the brief did not call out.
- **Collections use `{entity, count, items}`**, mirroring research/05 §5.3.
- **`PolicyEngine.actions_catalogue()` was added** beyond the four named methods:
  `/meta/actions` needs the JSON's action descriptions, and routes may not read the file.
- **`list_entries` raises `ValueError` on a bad filter value** even though the route
  validates first, so a direct caller cannot silently get `[]` back from a typo.
- **Validation is exhaustive, not fail-fast.** `PolicyLoadError.problems` lists every
  problem across all 110 rows, so fixing a broken table is one pass, not eleven.
- **I-4's "scheduling action" is read as the three retrying classes**, matching the
  wording of the Stage 1 gate. Only `RETRY_SCHEDULED` carries a positive wait in practice.
- `hatchling` build backend with `packages = ["src"]`, so `uv sync` installs the project.

### Deviations from the brief

The 404 decision above is the only one.

### On `error_policy.json`

Left untouched, and it needs no editing: all 110 rows pass the full validation pass, and
the counts match the brief exactly.

### Tests — 75 passing

They guarantee:

- The taxonomy is exactly what CLAUDE.md claims: 110 unique codes, all resolving, all
  eight actions present at the exact per-action counts, exactly 27 recoverable, and those
  27 are precisely the three retrying classes. Counts are hard-coded, not derived from
  the file under test.
- **I-2** — unknown codes raise, the exception carries `.code`, and the near misses
  `""`, `" "`, `INSUFFICIENT_FUNDS`, `insufficient-funds`, `insufficient_fund` all raise.
  If anyone later adds case folding or fuzzy matching, these fail.
- **I-4** — positive `min_wait_hours` appears only on retrying actions.
- **I-1** — `MODEL_ELIGIBLE_ACTIONS` is exactly `{RETRY_SCHEDULED, SWITCH_RAIL}`, is a
  strict subset of the retrying set, and no unrecoverable code is model-eligible.
- The validation pass bites: six tampered-table tests each fail at load, and the app
  **refuses to boot** on a broken table. Copies go to `tmp_path`; the real file is never
  written to.
- The 404 and 400 bodies carry the full six-key envelope and share no code or reason.

Verified against a real `uvicorn` process, not only `TestClient`: `/health` reports 110
codes, `/v1/errors/insufficient_funds` returns `RETRY_SCHEDULED` at 72h, coverage reports
27 of 110, `/v1/errors/fake` returns 404, `/docs` renders.

### Open questions carried into Stage 2

- **`RETRY_NOW` has no sub-hour representation.** `min_wait_hours` is an integer and is 0
  for all three `RETRY_NOW` codes. "Re-attempt within seconds" is not expressible in the
  table, so the Stage 2 scheduler needs its own floor for that class rather than reading
  one from policy. *(Stage 2: `RETRY_NOW_FLOOR_SECONDS = 30` in the executor.)*
- **The table is read once at startup and never reloaded**, so editing it needs a
  restart. **No auth**: research/05 specifies a Bearer key; the brief excluded it.
- **`BadErrorCodeError` is declared but unused** until `/v1/recovery/decide` exists.
  *(Stage 2: now raised by `/decide` and case creation.)*
- **`research/08-ui-spec.md` was not in CLAUDE.md's file map or index.** *(Stage 2: added.)*

---

## Stage 2 — Simulator and case engine · complete

### What was built

| Module | Responsible for |
|---|---|
| `src/store/schema.sql` | payments, cases, attempts, audit, downtimes. UNIQUE index on `attempts.idempotency_key`. No REAL column anywhere. |
| `src/store/db.py` | Connection, row dataclasses, derived ids, query helpers. `set_case_state` is the only path to `cases.state`. |
| `src/simulator/rails.py` | Rail inventory, `derive_rng`, IST peak window, the downtime feed in Razorpay's schema, `RailHealth` point-in-time lookup. |
| `src/simulator/declines.py` | CONFIG — every rate cited or marked as ours — and the causal cascade turning latent state into an error code. |
| `src/simulator/world.py` | Latent state and the one resolution method. Returns `AttemptOutcome` and nothing else. |
| `src/simulator/generate.py` | Deterministic population + CLI. Writes cases at RECEIVED with `arm` unset. |
| `src/executor/state.py` | Nine states, legal edges as data, audit-before-state `transition`. |
| `src/executor/runner.py` | Bounds, diagnosis routing, attempt execution, status polls, `decide`. Takes the world as an argument. |
| `src/api/deps.py` | Per-request connection and World; shared engine and runner. |
| `src/api/routes_cases.py` | `/decide`, cases, attempts, status-poll, stop. |
| `src/api/routes_rails.py` | `GET/POST /v1/rails/health`. |

### Decisions not specified in CLAUDE.md or research/

- **No server clock, so `now` is a required request field** on `/decide`, `/attempts`,
  `/status-poll` and `/stop`. Case creation uses `failed_at` — for a case being opened
  that *is* the current time, so a second field would be redundant.
- **`ESCALATED` and `STOPPED` have no edge back to `SCHEDULED`.** This makes I-4
  structural rather than conventional: the four non-retrying classes landing there cannot
  reach an attempt however a later caller misuses the runner. `AWAITING_STATUS` keeps its
  edge to `SCHEDULED`, gated on `status_resolved_at`, because I-6 requires it.
- **The idempotency check runs before the bounds check.** A client replaying a request
  must be told it is a duplicate, not told what state the original attempt left the case
  in. Reporting a replay as a 422 would hide a double-charge attempt.
- **A failed attempt re-diagnoses against the new code** and updates `cases.error_code`;
  the original stays on `payments.first_error_code` so Stage 3 can segment by the cause
  that opened the case — the trail research/05 §5.2's example shows.
- **After a poll resolves as `failed`, the case is scheduled at the 30s floor**; policy
  says nothing about post-resolution timing. **`min_interval` is that same floor**, not an
  hour-scale gate: `min_wait_hours` carries the real spacing.
- **IDs are derived from stable keys** (`blake2b` of the identifying parts), not counters
  or UUIDs. That is what makes a run byte-identical and `POST /cases` idempotent on
  `payment_id` without a lookup-then-insert race.
- **`SWITCH_RAIL` targets come from a fixed map**, not sampled — a rail choice varying per
  run would break arm parity. **Missing `Idempotency-Key` returns 400 in the envelope**,
  not FastAPI's 422 `detail` blob, so this surface has one failure shape.
- **The World is rebuilt per API request** from the downtimes currently in the store, so
  an injected outage changes the next attempt immediately. Latent state is derived from
  `(seed, customer_id)`, never accumulated, so this changes no outcome.
- **Simulator calibration.** Rates were tuned against the printed distribution until the
  stream spread across all eight classes with none above 35%: ~17% first-attempt failure,
  `insufficient_funds` 16% of failures, `SWITCH_RAIL` 9%. Tuned, not fitted — there is no
  dataset to fit to, and every knob sits in one CONFIG dict per module.

### Deviations from the brief

- **`test_no_wall_clock.py` parses the AST instead of grepping.** A text grep fires on
  `src/api/main.py`'s own docstring, which names `datetime.now()` to say it is banned, and
  would miss an aliased import. Four tests prove the detector bites and ignores prose.
- **`world.attempt` also resolves the original payment**, with `action="INITIAL"` and
  `error_code=None`, rather than the world growing a second entry point. Origination and
  retry are the same computation at different timestamps, so the interface stays single.
- **Status polls take the resolution from the caller** rather than asking the world, again
  keeping the world at exactly one resolution method.

### Two bugs the tests caught

- `ATTEMPTING -> ESCALATED` was missing, so any attempt failing with a nudge-class code
  wedged the case in SCHEDULED behind a plausible-looking 409. Added, plus a static test
  asserting every diagnosis destination is reachable from `DIAGNOSED` and `ATTEMPTING`.
- `DIAGNOSED -> EXHAUSTED` was missing, so a case whose first scheduled wait already
  exceeded `drop_dead_at` could not be closed.

### Tests — 303 passing (228 new)

- **I-5** — the UNIQUE index is asserted to exist and be unique; a raw SQL insert with a
  duplicate key raises `IntegrityError` with no application code in the way; over HTTP,
  201 then 409, and only one attempt row survives.
- **I-6** — all five AWAIT_STATUS codes land in `AWAITING_STATUS` with `scheduled_at`
  null; an attempt returns 423 and writes nothing; a resolver that raises on call proves
  the world is never consulted; after a poll, the same attempt returns 201.
- **I-7** — `max_attempts`, `drop_dead_at` and `min_interval` each 422, through the runner
  and over HTTP; a refused attempt writes no attempt row and no audit row.
- **I-8** — a stub resolver queries the audit table at the moment it is called and finds
  the `ATTEMPTING` row already there. Written from the response, it would find nothing.
- **I-12** — `AttemptOutcome` has four fields, no `__dict__`, and no field matching any of
  22 latent tokens; `src/policy/`, `src/executor/` and `src/arms/` are AST-scanned for any
  import of `src.simulator`.
- **I-13** — same seed gives a byte-identical database; resolving attempt 3 of one case is
  identical whether or not 50 other attempts ran first; adding 200 customers does not
  shift existing latent state. **No wall clock** — every `src/` file is parsed for clock
  calls.
- **Simulator** — 2000 payments over 30 days, all eight classes present, no code outside
  the 110, none above 35%, `card_expired` only on cards, `insufficient_funds` skewed late
  in the salary cycle.

Verified against a real uvicorn process: 110 codes loaded, 93 downtimes served,
`Idempotency-Key: same` twice → 201 then 409, an attempt on a `payment_pending` case → 423
then 201 after the poll, `/decide` on an unknown code → 400, `/docs` renders. Two runs at
seed 42 give byte-identical databases.

### Open questions carried into Stage 3

- **Nudges have no recovery path.** `nudge_responsiveness` sits in latent state but no
  Stage 2 code consumes it, because the executor never nudges. Stage 3's arms need one, or
  `NUDGE_CUSTOMER` — 23 of the 110 codes — can never recover in any arm.
- **No tick loop yet.** `Runner.run_due` executes what is due at one instant; walking the
  30-day window is `eval/run_arms.py`'s job.
- **`cases` does not carry `city_tier`, `vpa_handle` or `payer_bank`.** Observable and
  available from `world.customer_profile`, but Stage 4's features need them stored.
- **`psp_app_ not_available` has a space in it** in `error_policy.json`. Left alone as
  ground truth; the simulator does not emit it. Flag it if the table is regenerated.
- **The `bank_outage` scenario is untested end to end.** It generates and its
  high-severity window is asserted, but no arm has run against it.
