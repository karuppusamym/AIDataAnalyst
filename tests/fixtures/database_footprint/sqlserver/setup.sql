CREATE SCHEMA footprint_context_sample;
GO
CREATE TABLE footprint_context_sample.customers (
    customer_id int PRIMARY KEY,
    region varchar(20) NOT NULL
);
CREATE TABLE footprint_context_sample.orders (
    order_id int PRIMARY KEY,
    customer_id int NOT NULL REFERENCES footprint_context_sample.customers(customer_id),
    amount decimal(12,2) NOT NULL,
    discount decimal(12,2) NOT NULL
);
CREATE TABLE footprint_context_sample.customer_totals (
    customer_id int PRIMARY KEY,
    net_revenue decimal(12,2) NOT NULL
);
INSERT INTO footprint_context_sample.customers VALUES (1, 'East'), (2, 'West');
INSERT INTO footprint_context_sample.orders VALUES
    (101, 1, 100, 10), (102, 1, 60, 0), (103, 2, 80, 10);
GO
