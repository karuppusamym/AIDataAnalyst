IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'retail')
    EXEC('CREATE SCHEMA retail');
GO
IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'risk')
    EXEC('CREATE SCHEMA risk');
GO

IF OBJECT_ID('retail.customer', 'U') IS NULL
BEGIN
    CREATE TABLE retail.customer (
        customer_id BIGINT PRIMARY KEY,
        customer_name NVARCHAR(200) NOT NULL,
        state_code CHAR(2) NOT NULL,
        opened_at DATE NOT NULL,
        email_address NVARCHAR(320) NULL,
        is_active BIT NOT NULL DEFAULT 1
    );
END
GO

IF OBJECT_ID('retail.account', 'U') IS NULL
BEGIN
    CREATE TABLE retail.account (
        account_id BIGINT PRIMARY KEY,
        customer_id BIGINT NOT NULL REFERENCES retail.customer(customer_id),
        account_type NVARCHAR(50) NOT NULL,
        opened_at DATE NOT NULL,
        closed_at DATE NULL,
        current_balance DECIMAL(18, 2) NOT NULL
    );
END
GO

IF OBJECT_ID('retail.transaction_fact', 'U') IS NULL
BEGIN
    CREATE TABLE retail.transaction_fact (
        transaction_id BIGINT PRIMARY KEY,
        account_id BIGINT NOT NULL REFERENCES retail.account(account_id),
        transaction_timestamp DATETIME2 NOT NULL,
        amount DECIMAL(18, 2) NOT NULL,
        transaction_type NVARCHAR(20) NOT NULL,
        status NVARCHAR(20) NOT NULL
    );
END
GO

IF OBJECT_ID('risk.customer_risk_snapshot', 'U') IS NULL
BEGIN
    CREATE TABLE risk.customer_risk_snapshot (
        customer_id BIGINT NOT NULL REFERENCES retail.customer(customer_id),
        snapshot_date DATE NOT NULL,
        risk_rating NVARCHAR(20) NOT NULL,
        probability_of_default DECIMAL(8, 6) NULL,
        PRIMARY KEY (customer_id, snapshot_date)
    );
END
GO

IF NOT EXISTS (SELECT 1 FROM retail.customer)
BEGIN
    INSERT INTO retail.customer (customer_id, customer_name, state_code, opened_at, email_address, is_active)
    VALUES
        (1, 'Example Customer One', 'NY', '2020-01-10', 'one@example.invalid', 1),
        (2, 'Example Customer Two', 'TX', '2021-03-15', 'two@example.invalid', 1);
END
GO

IF NOT EXISTS (SELECT 1 FROM retail.account)
BEGIN
    INSERT INTO retail.account (account_id, customer_id, account_type, opened_at, closed_at, current_balance)
    VALUES
        (1001, 1, 'CHECKING', '2020-01-10', NULL, 2500.00),
        (1002, 2, 'SAVINGS', '2021-03-15', NULL, 9750.00);
END
GO

IF NOT EXISTS (SELECT 1 FROM retail.transaction_fact)
BEGIN
    INSERT INTO retail.transaction_fact (transaction_id, account_id, transaction_timestamp, amount, transaction_type, status)
    VALUES
        (90001, 1001, DATEADD(DAY, -2, SYSUTCDATETIME()), 125.00, 'DEBIT', 'COMPLETED'),
        (90002, 1002, DATEADD(DAY, -1, SYSUTCDATETIME()), 500.00, 'CREDIT', 'COMPLETED');
END
GO

IF NOT EXISTS (SELECT 1 FROM risk.customer_risk_snapshot)
BEGIN
    INSERT INTO risk.customer_risk_snapshot (customer_id, snapshot_date, risk_rating, probability_of_default)
    VALUES
        (1, CAST(SYSUTCDATETIME() AS DATE), 'LOW', 0.001500),
        (2, CAST(SYSUTCDATETIME() AS DATE), 'MEDIUM', 0.015000);
END
GO
