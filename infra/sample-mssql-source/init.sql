-- Payments domain: transactions posted against accounts, plus the payments
-- and disputes that reference them. account_id values here deliberately
-- overlap sample-source's customer.account.account_id (1001-1008) -- there is
-- no cross-engine FK, but the shared value-space is what lets the platform's
-- cross-source relationship detector find a real match instead of a name
-- coincidence.

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'payments')
    EXEC('CREATE SCHEMA payments');
GO

IF OBJECT_ID('payments.transaction_fact', 'U') IS NULL
BEGIN
    CREATE TABLE payments.transaction_fact (
        transaction_id BIGINT PRIMARY KEY,
        account_id BIGINT NOT NULL,
        posted_at DATETIME2 NOT NULL,
        amount_minor BIGINT NOT NULL,
        currency_code CHAR(3) NOT NULL,
        direction NVARCHAR(8) NOT NULL,
        merchant_category_code NVARCHAR(8) NULL,
        channel NVARCHAR(16) NOT NULL
    );
END
GO

IF OBJECT_ID('payments.payment', 'U') IS NULL
BEGIN
    CREATE TABLE payments.payment (
        payment_id BIGINT PRIMARY KEY,
        transaction_id BIGINT NOT NULL REFERENCES payments.transaction_fact(transaction_id),
        payment_method NVARCHAR(24) NOT NULL,
        status NVARCHAR(16) NOT NULL,
        initiated_at DATETIME2 NOT NULL,
        settled_at DATETIME2 NULL,
        counterparty_name NVARCHAR(200) NULL
    );
END
GO

IF OBJECT_ID('payments.dispute', 'U') IS NULL
BEGIN
    CREATE TABLE payments.dispute (
        dispute_id BIGINT PRIMARY KEY,
        transaction_id BIGINT NOT NULL REFERENCES payments.transaction_fact(transaction_id),
        reason_code NVARCHAR(24) NOT NULL,
        status NVARCHAR(16) NOT NULL,
        opened_at DATETIME2 NOT NULL,
        resolved_at DATETIME2 NULL,
        contact_email NVARCHAR(320) NULL
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM payments.transaction_fact)
BEGIN
    INSERT INTO payments.transaction_fact
        (transaction_id, account_id, posted_at, amount_minor, currency_code, direction, merchant_category_code, channel)
    VALUES
        (90001, 1001, DATEADD(DAY, -9, SYSUTCDATETIME()), 12500,  'USD', 'DEBIT',  '5411', 'CARD'),
        (90002, 1001, DATEADD(DAY, -7, SYSUTCDATETIME()), 45000,  'USD', 'CREDIT', NULL,   'ACH'),
        (90003, 1002, DATEADD(DAY, -6, SYSUTCDATETIME()), 200000, 'USD', 'CREDIT', NULL,   'WIRE'),
        (90004, 1003, DATEADD(DAY, -5, SYSUTCDATETIME()), 8999,   'USD', 'DEBIT',  '5812', 'CARD'),
        (90005, 1004, DATEADD(DAY, -5, SYSUTCDATETIME()), 350000, 'USD', 'DEBIT',  NULL,   'WIRE'),
        (90006, 1004, DATEADD(DAY, -4, SYSUTCDATETIME()), 15000,  'USD', 'DEBIT',  '5411', 'CARD'),
        (90007, 1005, DATEADD(DAY, -3, SYSUTCDATETIME()), 500000, 'USD', 'CREDIT', NULL,   'WIRE'),
        (90008, 1006, DATEADD(DAY, -3, SYSUTCDATETIME()), 3025,   'USD', 'DEBIT',  '5814', 'CARD'),
        (90009, 1007, DATEADD(DAY, -2, SYSUTCDATETIME()), 62000,  'USD', 'DEBIT',  NULL,   'ACH'),
        (90010, 1007, DATEADD(DAY, -1, SYSUTCDATETIME()), 9900,   'USD', 'DEBIT',  '5732', 'CARD'),
        (90011, 1008, DATEADD(DAY, -30, SYSUTCDATETIME()), 0,     'USD', 'DEBIT',  NULL,   'INTERNAL'),
        (90012, 1003, DATEADD(DAY, -1, SYSUTCDATETIME()), 4200,   'USD', 'DEBIT',  '5411', 'CARD');
END
GO

IF NOT EXISTS (SELECT 1 FROM payments.payment)
BEGIN
    INSERT INTO payments.payment
        (payment_id, transaction_id, payment_method, status, initiated_at, settled_at, counterparty_name)
    VALUES
        (70001, 90002, 'ACH_CREDIT',  'SETTLED', DATEADD(DAY, -7, SYSUTCDATETIME()), DATEADD(DAY, -6, SYSUTCDATETIME()), 'Acme Payroll Inc'),
        (70002, 90003, 'WIRE_CREDIT', 'SETTLED', DATEADD(DAY, -6, SYSUTCDATETIME()), DATEADD(DAY, -6, SYSUTCDATETIME()), 'Northwind Escrow LLC'),
        (70003, 90005, 'WIRE_DEBIT',  'SETTLED', DATEADD(DAY, -5, SYSUTCDATETIME()), DATEADD(DAY, -5, SYSUTCDATETIME()), 'Harbor Title Co'),
        (70004, 90007, 'WIRE_CREDIT', 'SETTLED', DATEADD(DAY, -3, SYSUTCDATETIME()), DATEADD(DAY, -3, SYSUTCDATETIME()), 'Meridian Capital'),
        (70005, 90009, 'ACH_DEBIT',   'PENDING', DATEADD(DAY, -2, SYSUTCDATETIME()), NULL, 'City Utilities Co'),
        (70006, 90011, 'INTERNAL_TRANSFER', 'SETTLED', DATEADD(DAY, -30, SYSUTCDATETIME()), DATEADD(DAY, -30, SYSUTCDATETIME()), NULL);
END
GO

IF NOT EXISTS (SELECT 1 FROM payments.dispute)
BEGIN
    INSERT INTO payments.dispute
        (dispute_id, transaction_id, reason_code, status, opened_at, resolved_at, contact_email)
    VALUES
        (60001, 90004, 'FRAUD_SUSPECTED',      'OPEN',   DATEADD(DAY, -2, SYSUTCDATETIME()), NULL, 'marcus.cole@example.invalid'),
        (60002, 90010, 'DUPLICATE_CHARGE',     'RESOLVED', DATEADD(DAY, -1, SYSUTCDATETIME()), SYSUTCDATETIME(), 'chidinma.okafor@example.invalid'),
        (60003, 90008, 'SERVICE_NOT_RECEIVED', 'OPEN',   DATEADD(DAY, -2, SYSUTCDATETIME()), NULL, 'jonas.weber@example.invalid');
END
GO
