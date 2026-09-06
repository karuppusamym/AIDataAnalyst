-- Executed by the sample-oracle-source-init sidecar service in compose.yaml, via
-- sqlplus connecting as SYS over the network to sample-oracle-source's FREEPDB1
-- pluggable database. NOT run through the image's /container-entrypoint-initdb.d
-- hook: gvenzl/oracle-free ships FREEPDB1 pre-baked into its seed data, so the
-- container's own first-boot "CREATE PLUGGABLE DATABASE FREEPDB1" step always
-- raises ORA-65012 (already exists); its recovery restart treats the database as
-- already initialized and skips /container-entrypoint-initdb.d entirely, so a
-- mounted init script there silently never runs. This sidecar retries the
-- connection until the listener is accepting queries, then always applies this
-- script directly, independent of that quirk.
--
-- Risk domain: point-in-time risk banding, credit exposure, and AML alerts.
-- customer_id/account_id values here deliberately overlap sample-source's
-- customer.customer/customer.account (1-5 / 1001-1008) -- there is no
-- cross-engine FK, but the shared value-space is what lets the platform's
-- cross-source relationship detector find a real match. Creates one
-- schema-owner user (risk) plus a read-only "source" user that mirrors the
-- credential the SqlServer/Postgres fixtures use, so AIDA_SAMPLE_ORACLE_SOURCE_DSN
-- authenticates as a least-privilege reader rather than a schema owner.

CREATE USER risk IDENTIFIED BY "Risk-Local-Only1";
GRANT CREATE SESSION, CREATE TABLE TO risk;
ALTER USER risk QUOTA UNLIMITED ON USERS;

CREATE USER source IDENTIFIED BY "source-local-only";
GRANT CREATE SESSION TO source;

CONNECT risk/"Risk-Local-Only1"@//sample-oracle-source:1521/FREEPDB1

CREATE TABLE risk.customer_risk_snapshot (
    snapshot_id NUMBER PRIMARY KEY,
    customer_id NUMBER NOT NULL,
    risk_band VARCHAR2(8) NOT NULL,
    pd_score_bucket VARCHAR2(16) NOT NULL,
    captured_at DATE NOT NULL
);

CREATE TABLE risk.account_exposure (
    exposure_id NUMBER PRIMARY KEY,
    account_id NUMBER NOT NULL,
    exposure_bucket VARCHAR2(16) NOT NULL,
    as_of_date DATE NOT NULL
);

CREATE TABLE risk.aml_alert (
    alert_id NUMBER PRIMARY KEY,
    customer_id NUMBER NOT NULL,
    alert_type VARCHAR2(24) NOT NULL,
    severity VARCHAR2(8) NOT NULL,
    status VARCHAR2(16) NOT NULL,
    raised_at DATE NOT NULL,
    closed_at DATE
);

INSERT INTO risk.customer_risk_snapshot (snapshot_id, customer_id, risk_band, pd_score_bucket, captured_at)
    VALUES (1, 1, 'LOW',      'P0_2',  TRUNC(SYSDATE));
INSERT INTO risk.customer_risk_snapshot (snapshot_id, customer_id, risk_band, pd_score_bucket, captured_at)
    VALUES (2, 2, 'MEDIUM',   'P2_5',  TRUNC(SYSDATE));
INSERT INTO risk.customer_risk_snapshot (snapshot_id, customer_id, risk_band, pd_score_bucket, captured_at)
    VALUES (3, 3, 'LOW',      'P0_2',  TRUNC(SYSDATE));
INSERT INTO risk.customer_risk_snapshot (snapshot_id, customer_id, risk_band, pd_score_bucket, captured_at)
    VALUES (4, 4, 'HIGH',     'P10_25',TRUNC(SYSDATE));
INSERT INTO risk.customer_risk_snapshot (snapshot_id, customer_id, risk_band, pd_score_bucket, captured_at)
    VALUES (5, 5, 'MEDIUM',   'P2_5',  TRUNC(SYSDATE));

INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (1, 1001, 'LOW',    TRUNC(SYSDATE));
INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (2, 1002, 'LOW',    TRUNC(SYSDATE));
INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (3, 1003, 'MEDIUM', TRUNC(SYSDATE));
INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (4, 1004, 'LOW',    TRUNC(SYSDATE));
INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (5, 1005, 'LOW',    TRUNC(SYSDATE));
INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (6, 1006, 'HIGH',   TRUNC(SYSDATE));
INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (7, 1007, 'MEDIUM', TRUNC(SYSDATE));
INSERT INTO risk.account_exposure (exposure_id, account_id, exposure_bucket, as_of_date)
    VALUES (8, 1008, 'LOW',    TRUNC(SYSDATE));

INSERT INTO risk.aml_alert (alert_id, customer_id, alert_type, severity, status, raised_at, closed_at)
    VALUES (1, 4, 'VELOCITY_THRESHOLD', 'MEDIUM', 'OPEN',   TRUNC(SYSDATE) - 2, NULL);
INSERT INTO risk.aml_alert (alert_id, customer_id, alert_type, severity, status, raised_at, closed_at)
    VALUES (2, 3, 'LARGE_CASH_EQUIVALENT', 'HIGH', 'CLOSED', TRUNC(SYSDATE) - 10, TRUNC(SYSDATE) - 8);

GRANT SELECT ON risk.customer_risk_snapshot TO source;
GRANT SELECT ON risk.account_exposure TO source;
GRANT SELECT ON risk.aml_alert TO source;

COMMIT;

EXIT;
