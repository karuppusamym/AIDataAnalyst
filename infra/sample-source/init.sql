CREATE SCHEMA IF NOT EXISTS retail;
CREATE SCHEMA IF NOT EXISTS risk;

CREATE TABLE retail.customer (
    customer_id BIGINT PRIMARY KEY,
    customer_name TEXT NOT NULL,
    state_code CHAR(2) NOT NULL,
    opened_at DATE NOT NULL,
    email_address TEXT,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE TABLE retail.account (
    account_id BIGINT PRIMARY KEY,
    customer_id BIGINT NOT NULL REFERENCES retail.customer(customer_id),
    account_type TEXT NOT NULL,
    opened_at DATE NOT NULL,
    closed_at DATE,
    current_balance NUMERIC(18, 2) NOT NULL
);

CREATE TABLE retail.transaction_fact (
    transaction_id BIGINT PRIMARY KEY,
    account_id BIGINT NOT NULL REFERENCES retail.account(account_id),
    transaction_timestamp TIMESTAMPTZ NOT NULL,
    amount NUMERIC(18, 2) NOT NULL,
    transaction_type TEXT NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE risk.customer_risk_snapshot (
    customer_id BIGINT NOT NULL REFERENCES retail.customer(customer_id),
    snapshot_date DATE NOT NULL,
    risk_rating TEXT NOT NULL,
    probability_of_default NUMERIC(8, 6),
    PRIMARY KEY (customer_id, snapshot_date)
);

INSERT INTO retail.customer VALUES
    (1, 'Example Customer One', 'NY', '2020-01-10', 'one@example.invalid', TRUE),
    (2, 'Example Customer Two', 'TX', '2021-03-15', 'two@example.invalid', TRUE);

INSERT INTO retail.account VALUES
    (1001, 1, 'CHECKING', '2020-01-10', NULL, 2500.00),
    (1002, 2, 'SAVINGS', '2021-03-15', NULL, 9750.00);

INSERT INTO retail.transaction_fact VALUES
    (90001, 1001, NOW() - INTERVAL '2 days', 125.00, 'DEBIT', 'COMPLETED'),
    (90002, 1002, NOW() - INTERVAL '1 day', 500.00, 'CREDIT', 'COMPLETED');

INSERT INTO risk.customer_risk_snapshot VALUES
    (1, CURRENT_DATE, 'LOW', 0.001500),
    (2, CURRENT_DATE, 'MEDIUM', 0.015000);

