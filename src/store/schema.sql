-- TRIAGE store — SQLite, written to be Postgres-portable.
--
-- Two things in here are load-bearing safety, not bookkeeping:
--
--   * attempts.idempotency_key carries a UNIQUE index. The double-charge guard is
--     a schema constraint, so a race cannot slip past an application-level `if`. (I-5)
--   * audit is append-only. Nothing in src/ ever issues UPDATE or DELETE against
--     it, and every state transition writes a row before its action executes. (I-8)
--
-- Money is INTEGER paise throughout. There is no REAL column in this file.
-- Timestamps are INTEGER unix seconds.

PRAGMA foreign_keys = ON;

-- The generated population. Written once per simulator run and never rewritten:
-- Stage 3 splits these across arms by assignment, it does not regenerate. (I-13)
CREATE TABLE IF NOT EXISTS payments (
    id                TEXT    PRIMARY KEY,
    customer_id       TEXT    NOT NULL,
    merchant_id       TEXT    NOT NULL,
    method            TEXT    NOT NULL,
    rail              TEXT    NOT NULL,
    amount_paise      INTEGER NOT NULL CHECK (amount_paise > 0),
    created_at        INTEGER NOT NULL,
    first_outcome     TEXT    NOT NULL CHECK (first_outcome IN ('success', 'failed')),
    first_error_code  TEXT
);

CREATE INDEX IF NOT EXISTS idx_payments_created_at ON payments (created_at);
CREATE INDEX IF NOT EXISTS idx_payments_outcome    ON payments (first_outcome);

-- One recovery case per failed payment. `arm` stays NULL until Stage 3 assigns it.
CREATE TABLE IF NOT EXISTS cases (
    id                      TEXT    PRIMARY KEY,
    payment_id              TEXT    NOT NULL UNIQUE REFERENCES payments (id),
    customer_id             TEXT    NOT NULL,
    merchant_id             TEXT    NOT NULL,
    method                  TEXT    NOT NULL,
    rail                    TEXT    NOT NULL,
    amount_paise            INTEGER NOT NULL CHECK (amount_paise > 0),
    error_code              TEXT    NOT NULL,
    error_source            TEXT,
    failed_at               INTEGER NOT NULL,
    state                   TEXT    NOT NULL,
    arm                     TEXT,
    max_attempts            INTEGER NOT NULL CHECK (max_attempts > 0),
    drop_dead_at            INTEGER NOT NULL,
    next_attempt_at         INTEGER,
    status_resolved_at      INTEGER,
    recovered_at            INTEGER,
    recovered_amount_paise  INTEGER,
    created_at              INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cases_state      ON cases (state);
CREATE INDEX IF NOT EXISTS idx_cases_arm        ON cases (arm);
CREATE INDEX IF NOT EXISTS idx_cases_error_code ON cases (error_code);
CREATE INDEX IF NOT EXISTS idx_cases_method     ON cases (method);
CREATE INDEX IF NOT EXISTS idx_cases_due        ON cases (state, next_attempt_at);

-- One row per executed action. Never updated in place after the outcome lands.
CREATE TABLE IF NOT EXISTS attempts (
    id               TEXT    PRIMARY KEY,
    case_id          TEXT    NOT NULL REFERENCES cases (id),
    attempt_number   INTEGER NOT NULL CHECK (attempt_number > 0),
    idempotency_key  TEXT    NOT NULL,
    action           TEXT    NOT NULL,
    target_rail      TEXT,
    scheduled_at     INTEGER,
    executed_at      INTEGER NOT NULL,
    outcome          TEXT    NOT NULL CHECK (outcome IN ('success', 'failed')),
    error_code       TEXT,
    latency_ms       INTEGER NOT NULL
);

-- I-5. The double-charge guard. Not application logic — a constraint.
CREATE UNIQUE INDEX IF NOT EXISTS idx_attempts_idempotency_key
    ON attempts (idempotency_key);

CREATE INDEX IF NOT EXISTS idx_attempts_case ON attempts (case_id, attempt_number);

-- Append-only. I-8: an action with no preceding audit row is a bug.
CREATE TABLE IF NOT EXISTS audit (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id          TEXT    NOT NULL REFERENCES cases (id),
    at               INTEGER NOT NULL,
    from_state       TEXT,
    to_state         TEXT    NOT NULL,
    actor            TEXT    NOT NULL,
    reason           TEXT    NOT NULL,
    idempotency_key  TEXT,
    detail_json      TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_case ON audit (case_id, id);

-- Mirrors the Razorpay Downtime API shape (research/05 §5.3). Simulated feed.
CREATE TABLE IF NOT EXISTS downtimes (
    id          TEXT    PRIMARY KEY,
    method      TEXT    NOT NULL,
    scope       TEXT    NOT NULL,
    instrument  TEXT,
    severity    TEXT    NOT NULL CHECK (severity IN ('low', 'medium', 'high')),
    status      TEXT    NOT NULL CHECK (status IN ('started', 'resolved')),
    begin       INTEGER NOT NULL,
    end         INTEGER
);

CREATE INDEX IF NOT EXISTS idx_downtimes_window ON downtimes (method, begin, end);
