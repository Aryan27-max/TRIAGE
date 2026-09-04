#!/usr/bin/env bash
# Exhaustive API verification against a REAL uvicorn process.
#
# TestClient passing while uvicorn fails is a real failure mode: the ASGI lifespan,
# the CORS middleware and the exception handlers all behave differently under a real
# server. Everything here goes over HTTP.
#
# Exits non-zero on the first category of failure, and prints a per-endpoint table.
#
#   scripts/verify_api.sh [--port 8099] [--keep-db]

set -uo pipefail

PORT="${PORT:-8099}"
BASE="http://127.0.0.1:${PORT}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Deliberately relative. The script cd's to the repo root, and a relative path is the
# one form both Git Bash's curl and a native Windows Python resolve identically.
WORK=".verify"
DB="${WORK}/verify.db"
LOG="${WORK}/uvicorn.log"
NOW=1737025200          # inside the standard simulated window
PASS=0
FAIL=0
RESULTS=""

cd "$ROOT"
rm -rf "$WORK" && mkdir -p "$WORK"

cleanup() {
  if [ -n "${SERVER_PID:-}" ]; then
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
  fi
  [ "${KEEP_DB:-0}" = "1" ] || rm -rf "$WORK"
}
trap cleanup EXIT

for arg in "$@"; do
  case "$arg" in
    --port=*) PORT="${arg#*=}"; BASE="http://127.0.0.1:${PORT}" ;;
    --keep-db) KEEP_DB=1 ;;
  esac
done

# ---------------------------------------------------------------- helpers ----

record() { # name status detail
  local mark
  if [ "$2" = "PASS" ]; then mark="PASS"; PASS=$((PASS + 1)); else mark="FAIL"; FAIL=$((FAIL + 1)); fi
  RESULTS="${RESULTS}${mark}|$1|${3:-}\n"
  printf '  %-4s %-52s %s\n' "$mark" "$1" "${3:-}"
}

# status <name> <expected-code> <curl args...>
status() {
  local name="$1" want="$2"; shift 2
  local got
  got="$(curl -s -o "${WORK}/body" -w '%{http_code}' "$@")"
  if [ "$got" = "$want" ]; then record "$name" PASS "$got"; else record "$name" FAIL "want $want got $got"; fi
}

# body_has <name> <jq-ish python expr on the last response> <description>
body_has() {
  local name="$1" expr="$2"
  if python -c "
import json,sys
d=json.load(open('${WORK}/body', encoding='utf-8'))
sys.exit(0 if ($expr) else 1)
" 2>/dev/null; then record "$name" PASS; else record "$name" FAIL "assertion: $expr"; fi
}

# Every error body must carry the full six-key Razorpay envelope.
envelope() {
  local name="$1"
  if python -c "
import json,sys
d=json.load(open('${WORK}/body', encoding='utf-8'))
want={'code','description','field','source','step','reason'}
sys.exit(0 if set(d.get('error',{}))==want else 1)
" 2>/dev/null; then record "$name" PASS; else record "$name" FAIL "envelope keys wrong"; fi
}

post() { curl -s -o "${WORK}/body" -w '%{http_code}' -X POST -H 'Content-Type: application/json' "$@"; }

section() { printf '\n%s\n' "$1"; }

# ------------------------------------------------------------------ boot ----

echo "TRIAGE API verification"
echo "  root  $ROOT"
echo "  db    $DB"
echo "  port  $PORT"

# A fresh store, plus a small population so the case endpoints have something real.
uv run python -m src.simulator.generate --n 400 --days 30 --seed 42 --db "$DB" >/dev/null 2>&1 \
  || { echo "FATAL: could not generate a store"; exit 1; }

TRIAGE_DB_PATH="$DB" uv run uvicorn src.api.main:app --host 127.0.0.1 --port "$PORT" >"$LOG" 2>&1 &
SERVER_PID=$!

if ! curl -s --retry 40 --retry-delay 1 --retry-connrefused -o /dev/null "${BASE}/health"; then
  echo "FATAL: server never came up. Log:"; cat "$LOG"; exit 1
fi

# ---------------------------------------------------------------- health ----

section "health and taxonomy"
status "GET /health" 200 "${BASE}/health"
body_has "  110 codes loaded" "d['policy_codes_loaded']==110"

status "GET /v1/errors" 200 "${BASE}/v1/errors"
body_has "  110 items" "d['count']==110"
status "GET /v1/errors?family=B" 200 "${BASE}/v1/errors?family=B"
body_has "  28 in family B" "d['count']==28"
status "GET /v1/errors?action=SWITCH_RAIL" 200 "${BASE}/v1/errors?action=SWITCH_RAIL"
body_has "  15 SWITCH_RAIL" "d['count']==15"
status "GET /v1/errors?recoverable=true" 200 "${BASE}/v1/errors?recoverable=true"
body_has "  27 recoverable" "d['count']==27"
status "GET /v1/errors?family=Z (bad)" 400 "${BASE}/v1/errors?family=Z"
envelope "  400 envelope"

status "GET /v1/errors/insufficient_funds" 200 "${BASE}/v1/errors/insufficient_funds"
body_has "  RETRY_SCHEDULED 72h" "d['action']=='RETRY_SCHEDULED' and d['min_wait_hours']==72"
status "GET /v1/errors/nope (unknown)" 404 "${BASE}/v1/errors/nope"
envelope "  404 envelope"

status "GET /v1/errors/meta/actions" 200 "${BASE}/v1/errors/meta/actions"
body_has "  8 action classes" "d['count']==8"
status "GET /v1/errors/meta/coverage" 200 "${BASE}/v1/errors/meta/coverage"
body_has "  27 of 110" "d['recoverable_codes']==27 and d['total_codes']==110"

# ---------------------------------------------------------------- decide ----

section "decide — one code per action class"
decide() { # code expected_action
  local got
  got="$(post "${BASE}/v1/recovery/decide" -d "{\"error_code\":\"$1\",\"now\":${NOW},\"method\":\"upi\"}")"
  if [ "$got" != "200" ]; then record "decide $1" FAIL "http $got"; return; fi
  body_has "decide $1 -> $2" "d['action']=='$2'"
}
decide request_timed_out          RETRY_NOW
decide insufficient_funds         RETRY_SCHEDULED
decide bank_technical_error       SWITCH_RAIL
decide card_expired               SWITCH_INSTRUMENT
decide incorrect_pin              NUDGE_CUSTOMER
decide payment_pending            AWAIT_STATUS
decide payment_risk_check_failed  STOP
decide invalid_amount             MERCHANT_ALERT

GOT="$(post "${BASE}/v1/recovery/decide" -d "{\"error_code\":\"not_a_code\",\"now\":${NOW}}")"
if [ "$GOT" = "400" ]; then record "decide unknown code -> 400" PASS; else record "decide unknown code -> 400" FAIL "$GOT"; fi
envelope "  400 envelope"
body_has "  reason unknown_error_code" "d['error']['reason']=='unknown_error_code'"

# I-4: none of the five non-retrying classes may carry a scheduled_at.
for code in card_expired incorrect_pin payment_pending payment_risk_check_failed invalid_amount; do
  post "${BASE}/v1/recovery/decide" -d "{\"error_code\":\"${code}\",\"now\":${NOW}}" >/dev/null
  body_has "I-4 ${code} scheduled_at null" "d['scheduled_at'] is None"
done

# ----------------------------------------------------------------- cases ----

section "case lifecycle"
CASE_BODY="{\"payment_id\":\"pay_VERIFY1\",\"error_code\":\"insufficient_funds\",\"method\":\"upi\",\"amount\":499000,\"failed_at\":${NOW},\"source\":\"bank\",\"customer\":{\"id\":\"cust_V\"},\"merchant\":{\"id\":\"mch_V\"}}"
GOT="$(post "${BASE}/v1/recovery/cases" -d "$CASE_BODY")"
if [ "$GOT" = "201" ]; then record "POST /cases -> 201" PASS; else record "POST /cases -> 201" FAIL "$GOT"; fi
CASE_ID="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['id'])")"
NEXT_AT="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['next_attempt_at'])")"
record "  case id" PASS "$CASE_ID"

GOT="$(post "${BASE}/v1/recovery/cases" -d "$CASE_BODY")"
if [ "$GOT" = "200" ]; then record "POST /cases duplicate -> 200 idempotent" PASS; else record "POST /cases duplicate" FAIL "$GOT"; fi

post "${BASE}/v1/recovery/cases" -d "{\"payment_id\":\"pay_BADCODE\",\"error_code\":\"nope\",\"method\":\"upi\",\"amount\":100,\"failed_at\":${NOW}}" >/dev/null
envelope "POST /cases unknown code envelope"

status "GET /v1/recovery/cases" 200 "${BASE}/v1/recovery/cases"
status "GET /v1/recovery/cases?state=SCHEDULED" 200 "${BASE}/v1/recovery/cases?state=SCHEDULED"
status "GET /v1/recovery/cases?error_code=insufficient_funds" 200 "${BASE}/v1/recovery/cases?error_code=insufficient_funds"
status "GET /v1/recovery/cases?method=upi" 200 "${BASE}/v1/recovery/cases?method=upi"
status "GET /v1/recovery/cases?state=NOPE (bad)" 400 "${BASE}/v1/recovery/cases?state=NOPE"
envelope "  400 envelope"

status "GET /v1/recovery/cases/{id}" 200 "${BASE}/v1/recovery/cases/${CASE_ID}"
body_has "  carries attempts + audit" "'attempts' in d and 'audit' in d and len(d['audit'])>=3"
status "GET /v1/recovery/cases/nope" 404 "${BASE}/v1/recovery/cases/case_nope"

# --------------------------------------------------------------- attempts ----

section "attempts — the money-safety guards"
GOT="$(post "${BASE}/v1/recovery/cases/${CASE_ID}/attempts" -H 'Idempotency-Key: verify-1' -d "{\"now\":${NEXT_AT}}")"
if [ "$GOT" = "201" ]; then record "POST /attempts -> 201" PASS; else record "POST /attempts -> 201" FAIL "$GOT"; fi

GOT="$(post "${BASE}/v1/recovery/cases/${CASE_ID}/attempts" -H 'Idempotency-Key: verify-1' -d "{\"now\":$((NEXT_AT + 60))}")"
if [ "$GOT" = "409" ]; then record "I-5 same Idempotency-Key -> 409" PASS; else record "I-5 same key -> 409" FAIL "$GOT"; fi
envelope "  409 envelope"
body_has "  IDEMPOTENCY_CONFLICT" "d['error']['code']=='IDEMPOTENCY_CONFLICT'"

GOT="$(post "${BASE}/v1/recovery/cases/${CASE_ID}/attempts" -d "{\"now\":${NEXT_AT}}")"
if [ "$GOT" = "400" ]; then record "missing Idempotency-Key -> 400" PASS; else record "missing key -> 400" FAIL "$GOT"; fi
envelope "  400 envelope"

# I-6: an AWAIT_STATUS case cannot be attempted until a poll resolves it.
post "${BASE}/v1/recovery/cases" -d "{\"payment_id\":\"pay_PEND\",\"error_code\":\"payment_pending\",\"method\":\"upi\",\"amount\":250000,\"failed_at\":${NOW},\"source\":\"bank\"}" >/dev/null
PEND_ID="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['id'])")"
body_has "AWAIT_STATUS case -> AWAITING_STATUS" "d['status']=='AWAITING_STATUS'"
GOT="$(post "${BASE}/v1/recovery/cases/${PEND_ID}/attempts" -H 'Idempotency-Key: pend-1' -d "{\"now\":$((NOW + 3600))}")"
if [ "$GOT" = "423" ]; then record "I-6 attempt on pending -> 423" PASS; else record "I-6 -> 423" FAIL "$GOT"; fi
envelope "  423 envelope"
body_has "  AWAITING_STATUS" "d['error']['code']=='AWAITING_STATUS'"

GOT="$(post "${BASE}/v1/recovery/cases/${PEND_ID}/status-poll" -d "{\"now\":$((NOW + 7200)),\"resolution\":\"failed\"}")"
if [ "$GOT" = "200" ]; then record "POST /status-poll -> 200" PASS; else record "POST /status-poll" FAIL "$GOT"; fi
body_has "  now SCHEDULED" "d['status']=='SCHEDULED' and d['status_resolved_at'] is not None"
POLL_AT="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['next_attempt_at'])")"
GOT="$(post "${BASE}/v1/recovery/cases/${PEND_ID}/attempts" -H 'Idempotency-Key: pend-2' -d "{\"now\":${POLL_AT}}")"
if [ "$GOT" = "201" ]; then record "  attempt allowed after poll -> 201" PASS; else record "  after poll" FAIL "$GOT"; fi

# I-7: drop_dead_at.
post "${BASE}/v1/recovery/cases" -d "{\"payment_id\":\"pay_DD\",\"error_code\":\"insufficient_funds\",\"method\":\"upi\",\"amount\":499000,\"failed_at\":${NOW},\"source\":\"bank\"}" >/dev/null
DD_ID="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['id'])")"
DD_AT="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['drop_dead_at'])")"
GOT="$(post "${BASE}/v1/recovery/cases/${DD_ID}/attempts" -H 'Idempotency-Key: dd-1' -d "{\"now\":$((DD_AT + 1))}")"
if [ "$GOT" = "422" ]; then record "I-7 past drop_dead_at -> 422" PASS; else record "I-7 drop_dead" FAIL "$GOT"; fi
envelope "  422 envelope"
body_has "  POLICY_VIOLATION" "d['error']['code']=='POLICY_VIOLATION'"

# I-7: max_attempts. Spend the budget directly in the store, then ask for one more.
post "${BASE}/v1/recovery/cases" -d "{\"payment_id\":\"pay_MAX\",\"error_code\":\"insufficient_funds\",\"method\":\"upi\",\"amount\":499000,\"failed_at\":${NOW},\"source\":\"bank\"}" >/dev/null
MAX_ID="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['id'])")"
MAX_AT="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['next_attempt_at'])")"
uv run python - "$DB" "$MAX_ID" "$NOW" <<'PY' >/dev/null
import sys
from src.store import db
conn = db.connect(sys.argv[1])
case_id, now = sys.argv[2], int(sys.argv[3])
for n in range(1, 5):
    db.insert_attempt(conn, db.Attempt(
        id=db.stable_id("att_", case_id, n), case_id=case_id, attempt_number=n,
        idempotency_key=f"{case_id}:{n}", action="RETRY_SCHEDULED", target_rail=None,
        scheduled_at=now, executed_at=now, outcome="failed",
        error_code="insufficient_funds", latency_ms=900))
conn.commit()
PY
GOT="$(post "${BASE}/v1/recovery/cases/${MAX_ID}/attempts" -H 'Idempotency-Key: max-9' -d "{\"now\":${MAX_AT}}")"
if [ "$GOT" = "422" ]; then record "I-7 past max_attempts -> 422" PASS; else record "I-7 max_attempts" FAIL "$GOT"; fi
body_has "  max_attempts_exceeded" "d['error']['reason']=='max_attempts_exceeded'"

GOT="$(post "${BASE}/v1/recovery/cases/${DD_ID}/stop" -d "{\"now\":$((NOW + 60)),\"reason\":\"customer_cancelled\"}")"
if [ "$GOT" = "200" ]; then record "POST /stop -> 200" PASS; else record "POST /stop" FAIL "$GOT"; fi
body_has "  STOPPED" "d['status']=='STOPPED'"

# ----------------------------------------------------------------- rails ----

section "rail health"
status "GET /v1/rails/health" 200 "${BASE}/v1/rails/health"
GOT="$(post "${BASE}/v1/rails/health" -d "{\"method\":\"card\",\"scope\":\"all\",\"severity\":\"high\",\"begin\":$((NOW - 3600)),\"end\":$((NOW + 86400))}")"
if [ "$GOT" = "201" ]; then record "POST /v1/rails/health inject -> 201" PASS; else record "POST /rails/health" FAIL "$GOT"; fi
status "  active at now" 200 "${BASE}/v1/rails/health?at=${NOW}"
body_has "  the injected event is live" "d['count']>=1 and any(i['severity']=='high' for i in d['items'])"
GOT="$(post "${BASE}/v1/rails/health" -d "{\"method\":\"upi\",\"severity\":\"catastrophic\",\"begin\":${NOW}}")"
if [ "$GOT" = "400" ]; then record "  bad severity -> 400" PASS; else record "  bad severity" FAIL "$GOT"; fi
envelope "  400 envelope"

# The injected outage must change what the next decision does. A SWITCH_RAIL case on
# upi routes to card; card is now high-severity, so baseline waits rather than
# switching into a second outage (research/02 §2.4).
uv run python - "$DB" "$NOW" <<'PY' > "${WORK}/railcheck" 2>&1
import sys
from src.arms.baseline import BaselineArm
from src.arms.base import CaseSnapshot
from src.policy.engine import PolicyEngine
from src.simulator.rails import RailHealth
from src.store import db

conn = db.connect(sys.argv[1]); now = int(sys.argv[2])
engine = PolicyEngine().load()
snap = CaseSnapshot(
    id="case_X", error_code="bank_technical_error", error_source="bank", method="upi",
    rail="@oksbi", amount_paise=499000, failed_at=now, state="RECEIVED", max_attempts=4,
    drop_dead_at=now + 7 * 86400, next_attempt_at=None, status_resolved_at=None,
    nudge_sent_at=None, attempt_count=0, last_attempt_at=None, city_tier=2,
    vpa_handle="@oksbi", payer_bank="SBIN",
)
quiet = BaselineArm().next_action(snap, engine, RailHealth.from_events([]), now)
live = BaselineArm().next_action(
    snap, engine, RailHealth.from_events(db.list_downtimes(conn)), now)
print("QUIET", quiet.reason_code, "LIVE", live.reason_code)
sys.exit(0 if quiet.reason_code != live.reason_code else 1)
PY
if [ $? -eq 0 ]; then
  record "  injected downtime changes the decision" PASS "$(cat "${WORK}/railcheck")"
else
  record "  injected downtime changes the decision" FAIL "$(cat "${WORK}/railcheck")"
fi

# ------------------------------------------------------------ simulation ----

section "simulation and evaluation"
GOT="$(post "${BASE}/v1/simulator/run" -d '{"n_payments":300,"days":20,"seed":11,"scenario":"normal","arms":["control","baseline"],"trailing_days":3}')"
if [ "$GOT" = "202" ] || [ "$GOT" = "503" ]; then
  record "POST /v1/simulator/run" PASS "$GOT $([ "$GOT" = "503" ] && echo '(read-only)')"
else
  record "POST /v1/simulator/run" FAIL "$GOT"
fi
if [ "$GOT" = "202" ]; then
  RUN_ID="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['run_id'])")"
else
  RUN_ID=""
fi

status "GET /v1/eval/runs" 200 "${BASE}/v1/eval/runs"
body_has "  at least one run" "d['count']>=1"
if [ -z "$RUN_ID" ]; then
  RUN_ID="$(python -c "import json;print(json.load(open('${WORK}/body',encoding='utf-8'))['items'][0]['run_id'])")"
fi

status "GET /v1/eval/report/{id}" 200 "${BASE}/v1/eval/report/${RUN_ID}"
body_has "  §5.4 shape" "set(d) >= {'run_id','measurement','arms','uplift','by_error_code'}"
body_has "  dedup declared" "d['measurement']['dedup']=='by_payment_final_outcome'"
body_has "  losing segments published" "'losing_segments' in d"
status "GET /v1/eval/report/nope" 404 "${BASE}/v1/eval/report/run_nope"
envelope "  404 envelope"

status "GET /docs" 200 "${BASE}/docs"
status "GET /openapi.json" 200 "${BASE}/openapi.json"

# ---------------------------------------------------------------- report ----

printf '\n----------------------------------------------------------------\n'
printf 'PASS %d   FAIL %d\n' "$PASS" "$FAIL"
if [ -n "${WRITE_RESULTS:-}" ]; then printf "$RESULTS" > "$WRITE_RESULTS"; fi
[ "$FAIL" -eq 0 ] || exit 1
echo "All API checks passed against a real uvicorn process."
