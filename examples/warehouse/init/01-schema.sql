-- Synthetic demo warehouse: two units, each in its own schema.
CREATE SCHEMA unit_a;
CREATE SCHEMA unit_b;

CREATE TABLE unit_a.orders (
    order_id        bigint PRIMARY KEY,
    customer_id     bigint NOT NULL,
    customer_email  text NOT NULL,
    ordered_at      timestamptz NOT NULL,
    status          text NOT NULL CHECK (status IN ('paid', 'cancelled', 'test')),
    channel         text NOT NULL,
    country         text NOT NULL,
    gross_amount    numeric(12, 2) NOT NULL,
    discount_amount numeric(12, 2) NOT NULL DEFAULT 0,
    is_internal     boolean NOT NULL DEFAULT false,
    is_first_order  boolean NOT NULL DEFAULT false
);
CREATE INDEX ON unit_a.orders (ordered_at);

CREATE TABLE unit_a.refunds (
    refund_id   bigint PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES unit_a.orders (order_id),
    refunded_at timestamptz NOT NULL,
    amount      numeric(12, 2) NOT NULL,
    reason      text NOT NULL
);

CREATE TABLE unit_a.products (
    product_id bigint PRIMARY KEY,
    name       text NOT NULL,
    is_active  boolean NOT NULL,
    updated_at timestamptz NOT NULL
);

CREATE TABLE unit_b.orders (LIKE unit_a.orders INCLUDING ALL);
CREATE TABLE unit_b.refunds (
    refund_id   bigint PRIMARY KEY,
    order_id    bigint NOT NULL REFERENCES unit_b.orders (order_id),
    refunded_at timestamptz NOT NULL,
    amount      numeric(12, 2) NOT NULL,
    reason      text NOT NULL
);
