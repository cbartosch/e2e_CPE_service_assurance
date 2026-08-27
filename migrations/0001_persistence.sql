-- 0001_persistence.sql -- the seven tables the specification names, minus the ones LangGraph owns.
--
-- The specification's Persistence section lists eight things to persist. One of them, LangGraph
-- checkpoints, is not here and must not be: `AsyncPostgresSaver.setup()` creates and version-tracks
-- its own tables (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`),
-- and a second definition of them in this file would drift from the library's the first time it
-- shipped a schema change. `persistence.checkpointer.checkpointer_scope` calls `setup()` on start
-- for exactly that reason.
--
-- Which of these tables has code behind it, today, honestly:
--
--   lpr_outbox_events        -- written and read by `persistence.outbox.PostgresOutbox`
--   lpr_schema_migrations    -- written and read by `persistence.migrations`
--   the other five           -- schema only
--
-- The five are here because a schema is a contract and the specification asks for it by name, and
-- because getting the shape agreed is most of the work of adding the code later. They are not here
-- to imply the code exists. Gap OUTBOX-3 carries that, and the implementation report repeats it
-- rather than leaving a reader to infer it from a migration file nobody reads.
--
-- Every table is prefixed `lpr_` so that a database shared with LangGraph's own tables, or with
-- another application, has no chance of a name collision on something as generic as `audit_events`.

CREATE TABLE IF NOT EXISTS lpr_schema_migrations (
    version     TEXT        PRIMARY KEY,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The checksum is what turns "this migration ran" into "this migration ran, and the file has
    -- not been edited since". Editing an applied migration is the failure this catches, and it is
    -- a common one: the edit looks applied on the machine that ran the original and is missing
    -- everywhere else.
    checksum    TEXT        NOT NULL
);

-- ------------------------------------------------------------------------------------------------
-- Outbox events. The one table with a relay behind it.
-- ------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lpr_outbox_events (
    event_id         TEXT        PRIMARY KEY,
    incident_id      TEXT        NOT NULL,
    -- The uniqueness that makes the whole pattern safe. A replayed node stages the same intent with
    -- the same key and `ON CONFLICT DO NOTHING` turns the duplicate into a no-op, which is why
    -- `enqueue` returning false is a normal outcome rather than an error.
    idempotency_key  TEXT        NOT NULL UNIQUE,
    target_system    TEXT        NOT NULL,
    action_type      TEXT        NOT NULL,
    target_ref       TEXT        NOT NULL,
    payload          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    created_at       TIMESTAMPTZ NOT NULL,
    status           TEXT        NOT NULL CHECK (status IN ('pending', 'sent', 'failed', 'dead')),
    attempts         INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at  TIMESTAMPTZ NOT NULL,
    last_error       TEXT        NOT NULL DEFAULT '',
    sent_at          TIMESTAMPTZ
);

-- The relay's only query: pending-or-failed, due, oldest first. Partial, because `sent` rows are
-- the overwhelming majority in a healthy system and indexing them would be paying for the rows the
-- query never wants.
CREATE INDEX IF NOT EXISTS lpr_outbox_due_idx
    ON lpr_outbox_events (next_attempt_at, created_at)
 WHERE status IN ('pending', 'failed');

CREATE INDEX IF NOT EXISTS lpr_outbox_incident_idx ON lpr_outbox_events (incident_id);

-- ------------------------------------------------------------------------------------------------
-- Idempotency records. Separate from the outbox on purpose.
-- ------------------------------------------------------------------------------------------------
-- The outbox records intents *this* system staged. This records effects the far side confirmed, and
-- the two differ after exactly the failure worth surviving: a call that reached the vendor and whose
-- response was lost. The outbox row is still `failed` and will retry; this row is what lets the
-- retry return the first result instead of applying a second reboot.
CREATE TABLE IF NOT EXISTS lpr_idempotency_records (
    idempotency_key  TEXT        PRIMARY KEY,
    incident_id      TEXT        NOT NULL,
    action_type      TEXT        NOT NULL,
    target_ref       TEXT        NOT NULL,
    attempt          INTEGER     NOT NULL DEFAULT 1,
    first_seen_at    TIMESTAMPTZ NOT NULL,
    completed_at     TIMESTAMPTZ,
    result           JSONB
);

-- ------------------------------------------------------------------------------------------------
-- Canonical incident index. Closes gap API-1 when something writes it.
-- ------------------------------------------------------------------------------------------------
-- A LangGraph thread that nobody has started has an empty state rather than raising, so "does this
-- incident exist?" has no answer without a table to ask. `api.app._known` currently 404s on an empty
-- state, which is the right answer by accident: it cannot tell "never existed" from "exists and is
-- empty".
CREATE TABLE IF NOT EXISTS lpr_incident_index (
    incident_id     TEXT        PRIMARY KEY,
    correlation_id  TEXT        NOT NULL,
    service_ref     TEXT        NOT NULL,
    customer_ref    TEXT        NOT NULL,
    case_type       TEXT        NOT NULL,
    technology      TEXT        NOT NULL,
    status          TEXT        NOT NULL,
    opened_at       TIMESTAMPTZ NOT NULL,
    closed_at       TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS lpr_incident_service_idx ON lpr_incident_index (service_ref);
CREATE INDEX IF NOT EXISTS lpr_incident_status_idx  ON lpr_incident_index (status, opened_at);

-- ------------------------------------------------------------------------------------------------
-- External-record links. Our id for their id.
-- ------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lpr_external_records (
    incident_id    TEXT        NOT NULL,
    system         TEXT        NOT NULL,
    record_type    TEXT        NOT NULL,
    external_ref   TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (incident_id, system, record_type, external_ref)
);

-- ------------------------------------------------------------------------------------------------
-- Audit events. Append-only by convention; there is no UPDATE path in any code that touches it.
-- ------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lpr_audit_events (
    event_id      TEXT        PRIMARY KEY,
    incident_id   TEXT        NOT NULL,
    sequence      BIGINT      NOT NULL,
    occurred_at   TIMESTAMPTZ NOT NULL,
    node          TEXT        NOT NULL,
    event_type    TEXT        NOT NULL,
    actor         TEXT        NOT NULL,
    reason_code   TEXT        NOT NULL DEFAULT '',
    detail        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- One sequence number per incident, so the trail has an order that does not depend on two rows
    -- having different timestamps. They will not: a super-step writes several at one instant.
    UNIQUE (incident_id, sequence)
);

CREATE INDEX IF NOT EXISTS lpr_audit_incident_idx ON lpr_audit_events (incident_id, sequence);

-- ------------------------------------------------------------------------------------------------
-- Approval history. Every answer, not the latest one.
-- ------------------------------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lpr_approval_history (
    approval_id     TEXT        PRIMARY KEY,
    incident_id     TEXT        NOT NULL,
    kind            TEXT        NOT NULL,
    status          TEXT        NOT NULL,
    decided_by      TEXT        NOT NULL,
    decided_by_role TEXT        NOT NULL,
    decided_at      TIMESTAMPTZ NOT NULL,
    rationale       TEXT        NOT NULL DEFAULT '',
    requested_at    TIMESTAMPTZ,
    expires_at      TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS lpr_approval_incident_idx ON lpr_approval_history (incident_id, decided_at);

-- ------------------------------------------------------------------------------------------------
-- KPI timestamps. The instants, not the derived durations.
-- ------------------------------------------------------------------------------------------------
-- Storing `time_to_detect_seconds` would store an arithmetic result that cannot be recomputed if the
-- definition changes, and two of the specification's KPIs are already not derivable from state. The
-- instants are the facts; the durations are a view over them.
CREATE TABLE IF NOT EXISTS lpr_kpi_timestamps (
    incident_id  TEXT        NOT NULL,
    milestone    TEXT        NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (incident_id, milestone)
);
