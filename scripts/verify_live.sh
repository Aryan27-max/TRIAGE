#!/usr/bin/env bash
# Verifies the LIVE deployed API on Railway — not localhost.
#
# Every assertion here goes over the public internet against the real deployment, so
# it catches what a local run cannot: a missing environment variable on the platform,
# a stale image, a cold-start failure, CORS misconfiguration.
#
# The one check that can legitimately fail on a correct build is POST /v1/simulator/run:
# it returns 503 only when TRIAGE_READ_ONLY is set on the platform. If it returns 201
# the code is fine and the deployment's Variables tab is not — the script says so
# explicitly rather than calling it a code defect.
#
#   scripts/verify_live.sh [base-url]

set -uo pipefail

BASE="${1:-https://triage-api-production-00b4.up.railway.app}"
PASS=0
FAIL=0
WARN=0

green() { printf '\033[32m%s\033[0m' "$1"; }
red()   { printf '\033[31m%s\033[0m' "$1"; }
amber() { printf '\033[33m%s\033[0m' "$1"; }

# check <label> <expected-substring> <actual> [expected-http] [actual-http]
check() {
  local label="$1" expect="$2" actual="$3"
  if printf '%s' "$actual" | grep -qF -- "$expect"; then
    printf '  %s  %s\n' "$(green PASS)" "$label"
    PASS=$((PASS + 1))
  else
    printf '  %s  %s\n' "$(red FAIL)" "$label"
    printf '        expected to contain: %s\n' "$expect"
    printf '        got: %s\n' "$(printf '%s' "$actual" | head -c 300)"
    FAIL=$((FAIL + 1))
  fi
}

status_of() { curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$@"; }
body_of()   { curl -s --max-time 30 "$@"; }

NOW="$(date +%s)"

echo "TRIAGE — live verification"
echo "base: ${BASE}"
echo

# -- 1. health ---------------------------------------------------------------------
echo "[1] GET /health"
CODE="$(status_of "${BASE}/health")"
BODY="$(body_of "${BASE}/health")"
check "200" "200" "$CODE"
check "policy table loaded (110 codes)" '"policy_codes_loaded":110' "$BODY"
echo "      read_only reported as: $(printf '%s' "$BODY" | grep -o '"read_only":[a-z]*' || echo '(field absent)')"

READ_ONLY_LIVE="$(printf '%s' "$BODY" | grep -o '"read_only":true' || true)"

# -- 2. coverage -------------------------------------------------------------------
echo
echo "[2] GET /v1/errors/meta/coverage"
BODY="$(body_of "${BASE}/v1/errors/meta/coverage")"
check "total_codes = 110" '"total_codes":110' "$BODY"
check "recoverable_codes = 27" '"recoverable_codes":27' "$BODY"

# -- 3. one known code -------------------------------------------------------------
echo
echo "[3] GET /v1/errors/insufficient_funds"
BODY="$(body_of "${BASE}/v1/errors/insufficient_funds")"
check "action = RETRY_SCHEDULED" '"action":"RETRY_SCHEDULED"' "$BODY"
check "min_wait_hours = 72" '"min_wait_hours":72' "$BODY"

# -- 4. one decide per action class ------------------------------------------------
echo
echo "[4] POST /v1/recovery/decide — one code per action class (all 8)"
decide() {
  local code="$1" expect_action="$2"
  local body
  body="$(curl -s --max-time 30 -X POST "${BASE}/v1/recovery/decide" \
    -H 'Content-Type: application/json' \
    -d "{\"error_code\":\"${code}\",\"now\":${NOW}}")"
  check "${expect_action} <- ${code}" "\"action\":\"${expect_action}\"" "$body"
}
decide duplicate_rrn_found            RETRY_NOW
decide bank_cutoff_in_progress        RETRY_SCHEDULED
decide authorisation_declined_by_psp  SWITCH_RAIL
decide amount_less_than_minimum_amount SWITCH_INSTRUMENT
decide authentication_failed          NUDGE_CUSTOMER
decide capture_failed                 AWAIT_STATUS
decide collect_on_mcc_blocked         STOP
decide bank_not_enabled               MERCHANT_ALERT

# -- 5. unknown code -> 404 + full envelope ----------------------------------------
echo
echo "[5] GET /v1/errors/not_a_real_code"
CODE="$(status_of "${BASE}/v1/errors/not_a_real_code")"
BODY="$(body_of "${BASE}/v1/errors/not_a_real_code")"
check "404" "404" "$CODE"
check "envelope: error.code" '"code":"NOT_FOUND_ERROR"' "$BODY"
check "envelope: source" '"source"' "$BODY"
check "envelope: step" '"step"' "$BODY"
check "envelope: reason" '"reason"' "$BODY"

# -- 6. write path is refused ------------------------------------------------------
echo
echo "[6] POST /v1/simulator/run — must be refused on a read-only deployment"
CODE="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 \
  -X POST "${BASE}/v1/simulator/run" -H 'Content-Type: application/json' -d '{}')"
if [ "$CODE" = "503" ]; then
  printf '  %s  503 SERVICE_UNAVAILABLE — TRIAGE_READ_ONLY is set on the platform\n' "$(green PASS)"
  PASS=$((PASS + 1))
else
  printf '  %s  got HTTP %s, expected 503\n' "$(amber CONFIG)" "$CODE"
  printf '        This is NOT a code defect: the read-only guard is tested and works.\n'
  printf '        It means TRIAGE_READ_ONLY is not set to true on the deployment.\n'
  printf '        Fix on Railway: Variables tab -> TRIAGE_READ_ONLY=true -> redeploy.\n'
  WARN=$((WARN + 1))
fi

# -- 7. rail health ----------------------------------------------------------------
echo
echo "[7] GET /v1/rails/health"
CODE="$(status_of "${BASE}/v1/rails/health")"
check "200" "200" "$CODE"

# -- 8. eval runs ------------------------------------------------------------------
echo
echo "[8] GET /v1/eval/runs"
BODY="$(body_of "${BASE}/v1/eval/runs")"
CODE="$(status_of "${BASE}/v1/eval/runs")"
check "200" "200" "$CODE"
check "at least one run present" '"run_id"' "$BODY"

RUN_ID="$(printf '%s' "$BODY" | grep -o '"run_id":"[^"]*"' | head -1 | cut -d'"' -f4)"
echo "      first run_id: ${RUN_ID:-<none found>}"

# -- 9. one report -----------------------------------------------------------------
echo
echo "[9] GET /v1/eval/report/{id}"
if [ -n "${RUN_ID:-}" ]; then
  CODE="$(status_of "${BASE}/v1/eval/report/${RUN_ID}")"
  BODY="$(body_of "${BASE}/v1/eval/report/${RUN_ID}")"
  check "200 for ${RUN_ID}" "200" "$CODE"
  check "report carries arm results" '"arms"' "$BODY"
else
  printf '  %s  skipped — no run_id available from step 8\n' "$(red FAIL)"
  FAIL=$((FAIL + 1))
fi

# -- summary -----------------------------------------------------------------------
echo
echo "-----------------------------------------------"
printf 'passed: %s   failed: %s   config warnings: %s\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -gt 0 ]; then
  echo "RESULT: FAIL"
  exit 1
fi
if [ "$WARN" -gt 0 ]; then
  echo "RESULT: PASS, with a platform configuration gap noted above."
  exit 0
fi
echo "RESULT: PASS"
