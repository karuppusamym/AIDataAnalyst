-- Executed by the gvenzl/oracle-free image's /container-entrypoint-initdb.d hook on
-- first container start, connected as SYS against the FREEPDB1 pluggable database.
-- Creates two schema-owner users (retail, risk) plus a read-only "source" user that
-- mirrors the credential the SqlServer/Postgres fixtures use, so AIDA_SAMPLE_ORACLE_SOURCE_DSN
-- authenticates as a least-privilege reader rather than a schema owner.

CREATE USER retail IDENTIFIED BY "Retail-Local-Only1";
GRANT CREATE SESSION, CREATE TABLE TO retail;
ALTER USER retail QUOTA UNLIMITED ON USERS;

CREATE USER risk IDENTIFIED BY "Risk-Local-Only1";
GRANT CREATE SESSION, CREATE TABLE TO risk;
ALTER USER risk QUOTA UNLIMITED ON USERS;

CREATE USER source IDENTIFIED BY "source-local-only";
GRANT CREATE SESSION TO source;

CONNECT retail/"Retail-Local-Only1"@//localhost:1521/FREEPDB1

CREATE TABLE retail.customer (
    customer_id NUMBER PRIMARY KEY,
    customer_name VARCHAR2(200) NOT NULL,
    state_code CHAR(2) NOT NULL,
    opened_at DATE NOT NULL,
    email_address VARCHAR2(320),
    is_active NUMBER(1) DEFAULT 1 NOT NULL
);

CREATE TABLE retail.account (
    account_id NUMBER PRIMARY KEY,
    customer_id NUMBER NOT NULL REFERENCES retail.customer(customer_id),
    account_type VARCHAR2(50) NOT NULL,
    opened_at DATE NOT NULL,
    closed_at DATE,
    current_balance NUMBER(18, 2) NOT NULL
);

CREATE TABLE retail.transaction_fact (
    transaction_id NUMBER PRIMARY KEY,
    account_id NUMBER NOT NULL REFERENCES retail.account(account_id),
    transaction_timestamp TIMESTAMP NOT NULL,
    amount NUMBER(18, 2) NOT NULL,
    transaction_type VARCHAR2(20) NOT NULL,
    status VARCHAR2(20) NOT NULL
);

INSERT INTO retail.customer (customer_id, customer_name, state_code, opened_at, email_address, is_active)
    VALUES (1, 'Example Customer One', 'NY', DATE '2020-01-10', 'one@example.invalid', 1);
INSERT INTO retail.customer (customer_id, customer_name, state_code, opened_at, email_address, is_active)
    VALUES (2, 'Example Customer Two', 'TX', DATE '2021-03-15', 'two@example.invalid', 1);

INSERT INTO retail.account (account_id, customer_id, account_type, opened_at, closed_at, current_balance)
    VALUES (1001, 1, 'CHECKING', DATE '2020-01-10', NULL, 2500.00);
INSERT INTO retail.account (account_id, customer_id, account_type, opened_at, closed_at, current_balance)
    VALUES (1002, 2, 'SAVINGS', DATE '2021-03-15', NULL, 9750.00);

INSERT INTO retail.transaction_fact (transaction_id, account_id, transaction_timestamp, amount, transaction_type, status)
    VALUES (90001, 1001, SYSTIMESTAMP - INTERVAL '2' DAY, 125.00, 'DEBIT', 'COMPLETED');
INSERT INTO retail.transaction_fact (transaction_id, account_id, transaction_timestamp, amount, transaction_type, status)
    VALUES (90002, 1002, SYSTIMESTAMP - INTERVAL '1' DAY, 500.00, 'CREDIT', 'COMPLETED');

GRANT SELECT ON retail.customer TO source;
GRANT SELECT ON retail.account TO source;
GRANT SELECT ON retail.transaction_fact TO source;
GRANT REFERENCES ON retail.customer TO risk;

COMMIT;

CONNECT risk/"Risk-Local-Only1"@//localhost:1521/FREEPDB1

CREATE TABLE risk.customer_risk_snapshot (
    customer_id NUMBER NOT NULL REFERENCES retail.customer(customer_id),
    snapshot_date DATE NOT NULL,
    risk_rating VARCHAR2(20) NOT NULL,
    probability_of_default NUMBER(8, 6),
    PRIMARY KEY (customer_id, snapshot_date)
);

INSERT INTO risk.customer_risk_snapshot (customer_id, snapshot_date, risk_rating, probability_of_default)
    VALUES (1, TRUNC(SYSDATE), 'LOW', 0.001500);
INSERT INTO risk.customer_risk_snapshot (customer_id, snapshot_date, risk_rating, probability_of_default)
    VALUES (2, TRUNC(SYSDATE), 'MEDIUM', 0.015000);

GRANT SELECT ON risk.customer_risk_snapshot TO source;

COMMIT;

EXIT;
