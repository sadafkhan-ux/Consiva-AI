-- Consiva AI Agent 3 — synthetic DSR demo dataset.
-- Fictional data, local/testing only. Nothing here is customer data.
--
-- Two deliberate changes from the version this was adapted from:
--
--   1. Everything is created in the `demo_customer` schema, not `public`. The
--      unqualified original would have created these tables alongside Consiva's own
--      and dropped anything in public that shared a name. The DSR source
--      authorization already points at `demo_customer`, so this is also the only
--      schema the connector is allowed to read.
--
--   2. The DROPs are schema-qualified for the same reason.
--
-- The dataset stands in for "the organisation's own systems". Consiva never holds
-- this data in production -- it reaches it through an authorized connector.

BEGIN;

CREATE SCHEMA IF NOT EXISTS demo_customer;

DROP TABLE IF EXISTS demo_customer.marketing_subscriptions;
DROP TABLE IF EXISTS demo_customer.support_tickets;
DROP TABLE IF EXISTS demo_customer.orders;
DROP TABLE IF EXISTS demo_customer.customers;

CREATE TABLE demo_customer.customers (
    id UUID PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    phone TEXT,
    address TEXT,
    date_of_birth DATE,
    account_status TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE demo_customer.orders (
    id UUID PRIMARY KEY,
    customer_email TEXT NOT NULL,
    order_id TEXT NOT NULL,
    amount NUMERIC(10,2) NOT NULL,
    order_status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE demo_customer.support_tickets (
    id UUID PRIMARY KEY,
    customer_email TEXT NOT NULL,
    issue TEXT NOT NULL,
    ticket_status TEXT NOT NULL,
    agent_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE demo_customer.marketing_subscriptions (
    id UUID PRIMARY KEY,
    customer_email TEXT NOT NULL,
    campaign_name TEXT NOT NULL,
    subscribed BOOLEAN NOT NULL DEFAULT TRUE,
    preference TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- TEST USER 1 — a complete access/mixed-action flow
INSERT INTO demo_customer.customers (id, name, email, phone, address, date_of_birth, account_status) VALUES
('11111111-1111-4111-8111-111111111111', 'Test User One', 'test.user@example.com', '+91-9000000001', '101 Demo Street, Gurugram', '1995-04-12', 'active');

INSERT INTO demo_customer.orders (id, customer_email, order_id, amount, order_status) VALUES
('21111111-1111-4111-8111-111111111111', 'test.user@example.com', 'ORD-DEMO-001', 2499.00, 'delivered'),
('21111111-1111-4111-8111-111111111112', 'test.user@example.com', 'ORD-DEMO-002', 1299.00, 'completed');

INSERT INTO demo_customer.support_tickets (id, customer_email, issue, ticket_status, agent_notes) VALUES
('31111111-1111-4111-8111-111111111111', 'test.user@example.com', 'Unable to update shipping address', 'closed', 'Customer requested address assistance.'),
('31111111-1111-4111-8111-111111111112', 'test.user@example.com', 'Order delivery delay', 'closed', 'Delivery partner issue resolved.');

INSERT INTO demo_customer.marketing_subscriptions (id, customer_email, campaign_name, subscribed, preference) VALUES
('41111111-1111-4111-8111-111111111111', 'test.user@example.com', 'Product Updates', TRUE, 'email'),
('41111111-1111-4111-8111-111111111112', 'test.user@example.com', 'Promotional Offers', TRUE, 'email');

-- TEST USER 2 — correction
INSERT INTO demo_customer.customers (id, name, email, phone, address, date_of_birth, account_status) VALUES
('12222222-2222-4222-8222-222222222222', 'Correction User', 'correction.user@example.com', '+91-9000000002', '202 Old Demo Road, Delhi', '1992-08-20', 'active');

-- TEST USER 3 — deletion
INSERT INTO demo_customer.customers (id, name, email, phone, address, date_of_birth, account_status) VALUES
('13333333-3333-4333-8333-333333333333', 'Delete User', 'delete.user@example.com', '+91-9000000003', '303 Sample Avenue, Noida', '1990-11-05', 'active');

INSERT INTO demo_customer.support_tickets (id, customer_email, issue, ticket_status, agent_notes) VALUES
('33333333-3333-4333-8333-333333333331', 'delete.user@example.com', 'Demo deletion request ticket', 'closed', 'Synthetic record for DSR deletion testing.');

INSERT INTO demo_customer.marketing_subscriptions (id, customer_email, campaign_name, subscribed, preference) VALUES
('43333333-3333-4333-8333-333333333331', 'delete.user@example.com', 'Demo Newsletter', TRUE, 'email');

-- TEST USER 4 — two people share one email. `customers` is configured as an identity
-- table, so this must stop for a human rather than the agent picking one.
INSERT INTO demo_customer.customers (id, name, email, phone, address, date_of_birth, account_status) VALUES
('14444444-4444-4444-8444-444444444441', 'Duplicate User A', 'duplicate.user@example.com', '+91-9000000011', '11 Test Lane, Gurugram', '1988-02-10', 'active'),
('14444444-4444-4444-8444-444444444442', 'Duplicate User B', 'duplicate.user@example.com', '+91-9000000012', '22 Test Lane, Gurugram', '1989-03-11', 'active');

-- TEST USER 5 — no.match@example.com has no records anywhere, on purpose.

COMMIT;

-- ── Quick test cases ────────────────────────────────────────────────────────────
--   ACCESS / mixed   test.user@example.com        7 records across 4 tables
--   CORRECTION       correction.user@example.com  1 record
--   DELETION         delete.user@example.com      3 records across 3 tables
--   MULTIPLE MATCH   duplicate.user@example.com   stops for human review
--   NO MATCH         no.match@example.com         explicit "nothing found"
