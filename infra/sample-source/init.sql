-- This container hosts two separate databases on the same Postgres server,
-- each registered as its own DataSource so cross-source lineage stays
-- genuine (a real cross-datasource relationship, not same-source): `bank_demo`
-- for the Customer domain (below) and `risk_demo` for the Risk domain
-- (created further down, after \connect). Payments (sample-mssql-source) is
-- the third domain, on a separate SQL Server container. All three reference
-- customer_id/account_id values without an enforced cross-engine/cross-database
-- FK -- that overlap is what lets the platform's cross-source relationship
-- detector find real matches.
--
-- `bank_demo` holds two schemas, not one: `customer`, the operational tables,
-- and `warehouse`, the enriched reporting estate the footprint product is
-- actually about -- views, routines with real bodies, lineage derivable from
-- those bodies, and columns whose meaning lives only in their comments. That
-- second schema exists because tracker row R11-FP13 scores answer correctness
-- over an *enriched* footprint and had none to score; the section that builds
-- it says which corpus case drove each object.

-- ============================================================
-- Customer domain (bank_demo, the default database on this server)
-- ============================================================

CREATE SCHEMA IF NOT EXISTS customer;

CREATE TABLE customer.customer (
    customer_id BIGINT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    state_code CHAR(2) NOT NULL,
    opened_at DATE NOT NULL,
    email_address TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE customer.account (
    account_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES customer.customer(customer_id),
    account_type TEXT NOT NULL,
    currency_code CHAR(3) NOT NULL,
    branch_code TEXT NOT NULL,
    status TEXT NOT NULL,
    opened_at DATE NOT NULL,
    closed_at DATE,
    current_balance NUMERIC(18, 2) NOT NULL
);

CREATE TABLE customer.card (
    card_id BIGINT PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES customer.account(account_id),
    -- Denormalized for card-issuance queries -- deliberately NOT a declared
    -- FK (unlike account_id above), so the platform's same-source
    -- relationship detector has a real, undeclared name+type match to find
    -- against customer.customer_id (EXACT_NAME_TYPE_TO_PRIMARY_KEY_V1).
    customer_id BIGINT NOT NULL,
    card_network TEXT NOT NULL,
    last4 CHAR(4) NOT NULL,
    status TEXT NOT NULL,
    issued_at DATE NOT NULL,
    expires_at DATE NOT NULL
);

INSERT INTO customer.customer VALUES
    (1, 'Ana Reyes',      'NY', '2020-01-10', 'ana.reyes@example.invalid',      TRUE),
    (2, 'Marcus Cole',    'TX', '2021-03-15', 'marcus.cole@example.invalid',    TRUE),
    (3, 'Priya Nair',     'CA', '2019-06-01', 'priya.nair@example.invalid',     TRUE),
    (4, 'Jonas Weber',    'IL', '2023-08-22', 'jonas.weber@example.invalid',    TRUE),
    (5, 'Chidinma Okafor','WA', '2022-05-30', 'chidinma.okafor@example.invalid',TRUE);

INSERT INTO customer.account VALUES
    (1001, 1, 'CHECKING', 'USD', 'BR-101', 'ACTIVE', '2020-01-10', NULL, 2500.00),
    (1002, 1, 'SAVINGS',  'USD', 'BR-101', 'ACTIVE', '2020-01-10', NULL, 18400.55),
    (1003, 2, 'CHECKING', 'USD', 'BR-204', 'ACTIVE', '2021-03-15', NULL, 975.10),
    (1004, 3, 'CHECKING', 'USD', 'BR-118', 'ACTIVE', '2019-06-01', NULL, 54210.00),
    (1005, 3, 'SAVINGS',  'USD', 'BR-118', 'ACTIVE', '2019-06-01', NULL, 132500.75),
    (1006, 4, 'CHECKING', 'USD', 'BR-330', 'ACTIVE', '2023-08-22', NULL, 300.25),
    (1007, 5, 'CHECKING', 'USD', 'BR-410', 'ACTIVE', '2022-05-30', NULL, 8120.40),
    (1008, 5, 'SAVINGS',  'USD', 'BR-410', 'CLOSED', '2022-05-30', '2025-01-15', 0.00);

INSERT INTO customer.card (card_id, account_id, customer_id, card_network, last4, status, issued_at, expires_at) VALUES
    (5001, 1001, 1, 'VISA',       '4412', 'ACTIVE', '2020-01-15', '2027-01-31'),
    (5002, 1003, 2, 'MASTERCARD', '5561', 'ACTIVE', '2021-03-20', '2026-03-31'),
    (5003, 1004, 3, 'VISA',       '4479', 'ACTIVE', '2019-06-05', '2027-06-30'),
    (5004, 1006, 4, 'VISA',       '4402', 'BLOCKED','2023-08-25', '2027-08-31'),
    (5005, 1007, 5, 'MASTERCARD', '5588', 'ACTIVE', '2022-06-01', '2026-06-30'),
    (5006, 1008, 5, 'VISA',       '4433', 'CANCELLED','2022-06-01', '2025-06-30');

-- === warehouse estate: begin (parsed by tests/test_answer_evaluation_estate.py) ===
-- ============================================================
-- Warehouse estate (bank_demo, schema `warehouse`)
-- ============================================================
-- Why this schema exists, and why it is shaped exactly like this.
--
-- The `customer` schema above is three tables, one declared FK and one
-- undeclared match: enough to demonstrate discovery and relationship
-- detection, and nothing at all for the *footprint* product -- no views, no
-- routines, no routine bodies to derive lineage from, and no column whose
-- meaning is anything other than its name. Tracker row R11-FP13 scores
-- "answer correctness over the enriched footprint" and had no enriched live
-- footprint to score, so `scripts/answer_evaluation_benchmark.py --live`
-- refused rather than grade the wrong estate. This schema is that estate.
--
-- It is built from the two corpora rather than invented:
-- `tests/fixtures/quality_benchmark_corpus/answer_evaluation_corpus.json` and
-- `footprint_enrichment_corpus.json`. Every object below is here because a
-- case names it -- the tables the questions expect an answer to stand on
-- (`fact_account_balances`, `fact_fraud_alerts`), the tables those are derived
-- from (`fact_payments`, `fact_loan_applications`), the routine whose reviewed
-- write lineage is the only path from its name to the table it writes
-- (`nightly_settlement_rollup`), and the routine whose write lineage must stay
-- an undecided proposal steering nothing (`quarterly_fee_accrual`). The names
-- match the slugs those corpora use, because the harness matches a live
-- estate's objects by name (`_normalise_table`/`_normalise_key` drop the
-- schema), so a differently-named live twin would score as a miss.
--
-- Three enrichment paths an answer cannot reach any other way:
--
--   1. `fact_payments` -> `fact_account_balances` exists **only** inside
--      `nightly_settlement_rollup`'s body. The two tables share no foreign
--      key, no constraint and no word stem, so the only way to know the
--      rollup produces the balance figures is to have parsed its body.
--   2. The abbreviated columns (`eod_ind`, `pstg_cyc_cd`, `stp_ind`,
--      `dq_bkt`, `ltv_bps`, `gl_cls`) mean nothing from their names. Their
--      `COMMENT ON COLUMN` text is the only place the meaning exists, which
--      is what makes a source comment a retrieval signal rather than
--      decoration.
--   3. `vw_account_balance_movement` joins across schemas into
--      `customer.account`, so view lineage has a real cross-schema edge
--      rather than a single-table projection.
--
-- Two things are deliberately *absent* from every name and comment below,
-- because the corpora are calibrated on their absence and the calibration is
-- the measurement: the published ontology concept's alias ("closing
-- position") and the steward-approved routine description ("cleared
-- interbank drafts each night"). If either phrase appeared here, the
-- questions that must be answerable only through the governed enrichment
-- would become answerable from a table name, and the corpus would silently
-- stop measuring what it claims to. The word "total" is kept out for the same
-- reason -- three questions use it. `tests/test_answer_evaluation_estate.py`
-- asserts that absence from the corpora themselves rather than from a list.
--
-- INV-6: this is a fixture, so the values are synthetic, but every column here
-- is an identifier, a code, a date or a minor-unit integer -- there is no name,
-- address or contact value anywhere in the schema, and nothing a control-plane
-- table could hold as a source value.

CREATE SCHEMA IF NOT EXISTS warehouse;

CREATE TABLE warehouse.dim_gl_account (
    gl_account_key BIGINT PRIMARY KEY,
    gl_account_cd TEXT NOT NULL,
    gl_account_name TEXT NOT NULL,
    gl_cls CHAR(1) NOT NULL
);

CREATE TABLE warehouse.fact_payments (
    payment_fact_id BIGINT PRIMARY KEY,
    -- Overlaps customer.account.account_id, deliberately without a declared
    -- FK: a warehouse fact does not constrain back to the operational table,
    -- and the undeclared match is what the same-source relationship detector
    -- has to find for itself.
    account_id BIGINT NOT NULL,
    gl_account_key BIGINT NOT NULL REFERENCES warehouse.dim_gl_account(gl_account_key),
    value_date DATE NOT NULL,
    settled_amt_minor BIGINT NOT NULL,
    returned_amt_minor BIGINT NOT NULL DEFAULT 0,
    currency_code CHAR(3) NOT NULL,
    pstg_cyc_cd CHAR(2) NOT NULL,
    stp_ind SMALLINT NOT NULL DEFAULT 1
);

CREATE TABLE warehouse.fact_account_balances (
    balance_fact_id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL,
    gl_account_key BIGINT NOT NULL REFERENCES warehouse.dim_gl_account(gl_account_key),
    as_of_date DATE NOT NULL,
    bal_amt_minor BIGINT NOT NULL,
    currency_code CHAR(3) NOT NULL,
    eod_ind SMALLINT NOT NULL DEFAULT 0
);

CREATE TABLE warehouse.fact_loan_applications (
    application_id BIGINT PRIMARY KEY,
    account_id BIGINT NOT NULL,
    submitted_on DATE NOT NULL,
    decision_status TEXT NOT NULL,
    origination_fee_minor BIGINT NOT NULL,
    dq_bkt CHAR(3) NOT NULL,
    ltv_bps INTEGER
);

CREATE TABLE warehouse.fact_fraud_alerts (
    alert_event_id BIGINT PRIMARY KEY,
    account_id BIGINT NOT NULL,
    alert_type TEXT NOT NULL,
    sev_lvl SMALLINT NOT NULL,
    raised_on DATE NOT NULL,
    detection_amt_minor BIGINT,
    case_status TEXT NOT NULL
);

CREATE TABLE warehouse.balance_load_audit (
    balance_load_audit_id BIGSERIAL PRIMARY KEY,
    balance_fact_id BIGINT NOT NULL,
    account_id BIGINT NOT NULL,
    loaded_on DATE NOT NULL
);

-- The table descriptions the connector ingests as `source_description`. Kept
-- terse and free of the two calibrated phrases: a table's own description must
-- not be a second path to it.
COMMENT ON TABLE warehouse.dim_gl_account IS
    'General-ledger account dimension; one row per posting account.';
COMMENT ON TABLE warehouse.fact_payments IS
    'Payments posted against an account, one row per payment instruction.';
COMMENT ON TABLE warehouse.fact_account_balances IS
    'One row per account per accounting day per general-ledger account, loaded by a batch job rather than by the application.';
COMMENT ON TABLE warehouse.fact_loan_applications IS
    'Loan applications and the decision made on each, one row per application.';
COMMENT ON TABLE warehouse.fact_fraud_alerts IS
    'Fraud alert events raised by the monitoring system.';
COMMENT ON TABLE warehouse.balance_load_audit IS
    'Append-only record of which balance rows a batch load wrote, written by a trigger.';

-- The columns whose names carry no meaning. This text is the only place the
-- meaning exists in the source, which is the whole point -- and
-- `tests/test_answer_evaluation_estate.py` fails if an abbreviated column is
-- added below without one.
COMMENT ON COLUMN warehouse.dim_gl_account.gl_account_cd IS
    'The chart-of-accounts code as Finance quotes it; the surrogate key is internal to this schema and never appears on a journal.';
COMMENT ON COLUMN warehouse.dim_gl_account.gl_cls IS
    'General-ledger classification: A asset, L liability, I income, E expense. Decides which side of the ledger an amount lands on.';
COMMENT ON COLUMN warehouse.fact_payments.pstg_cyc_cd IS
    'Batch the instruction was posted in: D0 same day, D1 next day, D2 two days out. A value of D2 means the money moved before the row appeared here.';
COMMENT ON COLUMN warehouse.fact_payments.stp_ind IS
    '1 when the instruction completed with no manual repair; 0 when an operator had to intervene. Repaired instructions are excluded when balances are loaded.';
COMMENT ON COLUMN warehouse.fact_payments.settled_amt_minor IS
    'Amount in the currency''s minor unit (cents for USD), always positive; direction is carried by the general-ledger account, not by the sign.';
COMMENT ON COLUMN warehouse.fact_account_balances.eod_ind IS
    '1 marks the row carrying the balance as at the end of the accounting day; 0 marks an intraday revision that a later row supersedes. Only rows with 1 are reportable.';
COMMENT ON COLUMN warehouse.fact_account_balances.bal_amt_minor IS
    'Balance in the currency''s minor unit, signed: a negative value is a debit balance on a liability account.';
COMMENT ON COLUMN warehouse.fact_loan_applications.dq_bkt IS
    'Delinquency bucket at decision time: CUR up to date, B30, B60 or B90 days past due on any existing facility.';
COMMENT ON COLUMN warehouse.fact_loan_applications.ltv_bps IS
    'Loan-to-value at decision, in basis points: 7500 is 75.00 per cent.';
COMMENT ON COLUMN warehouse.fact_fraud_alerts.sev_lvl IS
    'Severity 1 to 5, where 1 is informational and 5 stops the account. Only 4 and 5 are worked the same day.';

-- A view over three tables, one of them in another schema, so view lineage has
-- a real cross-schema edge to derive. Named without the stem of any question
-- whose target must be reachable only through the governed enrichment.
CREATE VIEW warehouse.vw_account_balance_movement AS
SELECT b.account_id,
       a.branch_code,
       g.gl_cls,
       b.as_of_date,
       b.bal_amt_minor,
       b.currency_code
FROM warehouse.fact_account_balances b
JOIN customer.account a ON a.account_id = b.account_id
JOIN warehouse.dim_gl_account g ON g.gl_account_key = b.gl_account_key
WHERE b.eod_ind = 1;

COMMENT ON VIEW warehouse.vw_account_balance_movement IS
    'Reportable balance rows joined to the branch that owns the account and the general-ledger classification.';

CREATE VIEW warehouse.vw_loan_pipeline_stage_mix AS
SELECT la.decision_status,
       la.dq_bkt,
       count(*) AS application_count,
       SUM(la.origination_fee_minor) AS origination_fee_minor
FROM warehouse.fact_loan_applications la
GROUP BY la.decision_status, la.dq_bkt;

COMMENT ON VIEW warehouse.vw_loan_pipeline_stage_mix IS
    'Application counts and origination charges by decision and delinquency bucket.';

-- A materialized view over the view above, so lineage has a two-hop path
-- (table -> view -> materialized view) rather than only direct projections,
-- and so the connector has a MATERIALIZED_VIEW object to discover at all.
CREATE MATERIALIZED VIEW warehouse.mv_ledger_control_summary AS
SELECT m.gl_cls,
       m.as_of_date,
       m.currency_code,
       SUM(m.bal_amt_minor) AS ledger_amt_minor
FROM warehouse.vw_account_balance_movement m
GROUP BY m.gl_cls, m.as_of_date, m.currency_code;

-- The batch job whose body is the only evidence that it produces the balance
-- figures: it reads fact_payments and writes fact_account_balances, and the
-- two tables are otherwise unconnected. Its source comment is deliberately
-- the kind of comment a real bank leaves -- an owner and a runbook number,
-- saying nothing about what the job does. That is why a steward-authored,
-- reviewed description is worth having, and it keeps the reviewed description
-- the only path for the question that looks for one.
CREATE PROCEDURE warehouse.nightly_settlement_rollup()
AS $$
BEGIN
    INSERT INTO warehouse.fact_account_balances
        (account_id, gl_account_key, as_of_date, bal_amt_minor, currency_code, eod_ind)
    SELECT p.account_id,
           p.gl_account_key,
           p.value_date,
           SUM(p.settled_amt_minor - p.returned_amt_minor),
           p.currency_code,
           1
    FROM warehouse.fact_payments p
    WHERE p.stp_ind = 1
    GROUP BY p.account_id, p.gl_account_key, p.value_date, p.currency_code;
END;
$$ LANGUAGE plpgsql;

COMMENT ON PROCEDURE warehouse.nightly_settlement_rollup() IS
    'Batch job. Owner: Ops Engineering. See runbook OPS-114.';

-- The gap case's routine. Its write reaches fact_fraud_alerts through a temp
-- table, so the derived edge is transitive rather than direct -- and on a live
-- estate that edge arrives PROPOSED, like every discovered edge, until a
-- steward decides it. Leaving it undecided is what the corpus's gap case
-- scores: an answer that stands on fact_fraud_alerts by this path has been
-- steered by lineage nobody approved.
CREATE PROCEDURE warehouse.quarterly_fee_accrual()
AS $$
BEGIN
    CREATE TEMP TABLE accrual_stage ON COMMIT DROP AS
    SELECT la.application_id, la.account_id, la.origination_fee_minor, la.dq_bkt
    FROM warehouse.fact_loan_applications la
    WHERE la.decision_status = 'APPROVED';

    INSERT INTO warehouse.fact_fraud_alerts
        (alert_event_id, account_id, alert_type, sev_lvl, raised_on,
         detection_amt_minor, case_status)
    SELECT s.application_id,
           s.account_id,
           'FEE_ACCRUAL_ANOMALY',
           2,
           DATE '2026-07-01',
           s.origination_fee_minor,
           'OPEN'
    FROM accrual_stage s
    WHERE s.dq_bkt <> 'CUR';
END;
$$ LANGUAGE plpgsql;

COMMENT ON PROCEDURE warehouse.quarterly_fee_accrual() IS
    'Quarter-end job. Owner: Finance Systems. See runbook FIN-207.';

-- A read-only routine, so the estate holds one routine a read-tool blueprint
-- could legitimately be generated from, next to two that write and therefore
-- cannot be.
CREATE FUNCTION warehouse.read_balance_movement()
RETURNS TABLE (account_id BIGINT, branch_code TEXT, gl_cls CHAR, bal_amt_minor BIGINT)
AS $$
    SELECT m.account_id, m.branch_code, m.gl_cls, m.bal_amt_minor
    FROM warehouse.vw_account_balance_movement m;
$$ LANGUAGE sql;

COMMENT ON FUNCTION warehouse.read_balance_movement() IS
    'Returns the reportable balance rows. Read only.';

-- A trigger, and with the BIGSERIAL keys above a sequence: both are object
-- kinds the connector learned to discover in this cycle and neither had a live
-- estate to be discovered in.
CREATE FUNCTION warehouse.fn_balance_load_audit() RETURNS trigger
AS $$
BEGIN
    INSERT INTO warehouse.balance_load_audit (balance_fact_id, account_id, loaded_on)
    VALUES (NEW.balance_fact_id, NEW.account_id, NEW.as_of_date);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_balance_load_audit
AFTER INSERT ON warehouse.fact_account_balances
FOR EACH ROW EXECUTE FUNCTION warehouse.fn_balance_load_audit();

-- Rows, so an answer over this estate has a result set to be right or wrong
-- about: the answer corpus's one gold_sql counts fact_fraud_alerts, which the
-- metadata-only fixture catalog can never execute. Fixed dates rather than
-- CURRENT_DATE, so two runs of this file produce the same estate.

INSERT INTO warehouse.dim_gl_account VALUES
    (9001, 'GL-1010', 'Demand deposits',      'L'),
    (9002, 'GL-1020', 'Savings deposits',     'L'),
    (9003, 'GL-4010', 'Interchange income',   'I'),
    (9004, 'GL-5010', 'Operating expense',    'E');

INSERT INTO warehouse.fact_payments VALUES
    (80001, 1001, 9001, '2026-06-29',  12500,     0, 'USD', 'D0', 1),
    (80002, 1001, 9001, '2026-06-30',  45000,     0, 'USD', 'D1', 1),
    (80003, 1002, 9002, '2026-06-30', 200000,     0, 'USD', 'D1', 1),
    (80004, 1003, 9001, '2026-06-30',   8999,  1200, 'USD', 'D0', 0),
    (80005, 1004, 9001, '2026-06-30', 350000,     0, 'USD', 'D2', 1),
    (80006, 1005, 9002, '2026-06-30', 500000,     0, 'USD', 'D1', 1),
    (80007, 1006, 9001, '2026-06-30',   3025,     0, 'USD', 'D0', 1),
    (80008, 1007, 9001, '2026-06-30',  62000,  9900, 'USD', 'D1', 1),
    (80009, 1007, 9003, '2026-06-30',    450,     0, 'USD', 'D0', 1),
    (80010, 1008, 9002, '2026-06-30',      0,     0, 'USD', 'D0', 0);

INSERT INTO warehouse.fact_account_balances
    (balance_fact_id, account_id, gl_account_key, as_of_date, bal_amt_minor, currency_code, eod_ind)
VALUES
    (7001, 1001, 9001, '2026-06-30',    250000, 'USD', 1),
    (7002, 1002, 9002, '2026-06-30',   1840055, 'USD', 1),
    (7003, 1003, 9001, '2026-06-30',     97510, 'USD', 1),
    (7004, 1004, 9001, '2026-06-30',   5421000, 'USD', 1),
    (7005, 1005, 9002, '2026-06-30',  13250075, 'USD', 1),
    (7006, 1006, 9001, '2026-06-30',     30025, 'USD', 1),
    (7007, 1007, 9001, '2026-06-30',    812040, 'USD', 1),
    (7008, 1008, 9002, '2026-06-30',         0, 'USD', 1),
    (7009, 1004, 9001, '2026-06-30',   5400000, 'USD', 0);

SELECT setval('warehouse.fact_account_balances_balance_fact_id_seq', 8000, true);

INSERT INTO warehouse.fact_loan_applications VALUES
    (3001, 1001, '2026-05-04', 'APPROVED', 45000, 'CUR', 6800),
    (3002, 1003, '2026-05-18', 'APPROVED', 62500, 'B30', 8200),
    (3003, 1004, '2026-06-02', 'DECLINED', 30000, 'B60', 9500),
    (3004, 1005, '2026-06-11', 'APPROVED', 88000, 'CUR', 5400),
    (3005, 1006, '2026-06-19', 'APPROVED', 27500, 'B90', 9900),
    (3006, 1007, '2026-06-24', 'WITHDRAWN', 0,    'CUR', NULL);

INSERT INTO warehouse.fact_fraud_alerts VALUES
    (8001, 1003, 'CARD_NOT_PRESENT_VELOCITY', 4, '2026-06-26',   8999, 'OPEN'),
    (8002, 1004, 'WIRE_BENEFICIARY_CHANGE',   5, '2026-06-27', 350000, 'OPEN'),
    (8003, 1006, 'DEVICE_REPUTATION',         2, '2026-06-28',   3025, 'CLOSED'),
    (8004, 1007, 'ACH_RETURN_PATTERN',        3, '2026-06-29',  62000, 'CLOSED');

REFRESH MATERIALIZED VIEW warehouse.mv_ledger_control_summary;
-- === warehouse estate: end ===

-- ============================================================
-- Risk domain (risk_demo, a second database on this same server)
-- ============================================================
-- A separate CREATE DATABASE + \connect, not a schema under bank_demo, so
-- Risk registers as its own DataSource (its own credential_reference/DSN)
-- and cross-source relationship discovery has two genuinely different
-- sources to pair, exactly as it does against sample-mssql-source.

CREATE DATABASE risk_demo;

\connect risk_demo

CREATE SCHEMA IF NOT EXISTS risk;

CREATE TABLE risk.customer_risk_snapshot (
    snapshot_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    risk_band TEXT NOT NULL,
    pd_score_bucket TEXT NOT NULL,
    captured_at DATE NOT NULL
);

CREATE TABLE risk.account_exposure (
    exposure_id BIGINT PRIMARY KEY,
    account_id BIGINT NOT NULL,
    exposure_bucket TEXT NOT NULL,
    as_of_date DATE NOT NULL
);

CREATE TABLE risk.aml_alert (
    alert_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL,
    alert_type TEXT NOT NULL,
    severity TEXT NOT NULL,
    status TEXT NOT NULL,
    raised_at DATE NOT NULL,
    closed_at DATE
);

INSERT INTO risk.customer_risk_snapshot VALUES
    (1, 1, 'LOW',    'P0_2',   CURRENT_DATE),
    (2, 2, 'MEDIUM', 'P2_5',   CURRENT_DATE),
    (3, 3, 'LOW',    'P0_2',   CURRENT_DATE),
    (4, 4, 'HIGH',   'P10_25', CURRENT_DATE),
    (5, 5, 'MEDIUM', 'P2_5',   CURRENT_DATE);

INSERT INTO risk.account_exposure VALUES
    (1, 1001, 'LOW',    CURRENT_DATE),
    (2, 1002, 'LOW',    CURRENT_DATE),
    (3, 1003, 'MEDIUM', CURRENT_DATE),
    (4, 1004, 'LOW',    CURRENT_DATE),
    (5, 1005, 'LOW',    CURRENT_DATE),
    (6, 1006, 'HIGH',   CURRENT_DATE),
    (7, 1007, 'MEDIUM', CURRENT_DATE),
    (8, 1008, 'LOW',    CURRENT_DATE);

INSERT INTO risk.aml_alert VALUES
    (1, 4, 'VELOCITY_THRESHOLD',    'MEDIUM', 'OPEN',   CURRENT_DATE - 2,  NULL),
    (2, 3, 'LARGE_CASH_EQUIVALENT', 'HIGH',   'CLOSED', CURRENT_DATE - 10, CURRENT_DATE - 8);
