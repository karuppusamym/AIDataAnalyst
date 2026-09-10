-- This container hosts two separate databases on the same Postgres server,
-- each registered as its own DataSource so cross-source lineage stays
-- genuine (a real cross-datasource relationship, not same-source): `bank_demo`
-- for the Customer domain (below) and `risk_demo` for the Risk domain
-- (created further down, after \connect). Payments (sample-mssql-source) is
-- the third domain, on a separate SQL Server container. All three reference
-- customer_id/account_id values without an enforced cross-engine/cross-database
-- FK -- that overlap is what lets the platform's cross-source relationship
-- detector find real matches.

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
