-- Customer domain: the system of record for who a customer is and what
-- accounts/cards they hold. Payments (sample-mssql-source) and Risk
-- (sample-oracle-source) both reference customer_id/account_id values from
-- this database without an enforced cross-engine FK -- that overlap is what
-- lets the platform's cross-source relationship detector find real matches.

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

INSERT INTO customer.card VALUES
    (5001, 1001, 'VISA',       '4412', 'ACTIVE', '2020-01-15', '2027-01-31'),
    (5002, 1003, 'MASTERCARD', '5561', 'ACTIVE', '2021-03-20', '2026-03-31'),
    (5003, 1004, 'VISA',       '4479', 'ACTIVE', '2019-06-05', '2027-06-30'),
    (5004, 1006, 'VISA',       '4402', 'BLOCKED','2023-08-25', '2027-08-31'),
    (5005, 1007, 'MASTERCARD', '5588', 'ACTIVE', '2022-06-01', '2026-06-30'),
    (5006, 1008, 'VISA',       '4433', 'CANCELLED','2022-06-01', '2025-06-30');
