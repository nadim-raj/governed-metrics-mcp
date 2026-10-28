-- Deterministic synthetic data, relative to the day the container is created.
-- Every email is on example.com. Re-create the container to move the data forward.
--
-- One deliberate fault: unit_b's refunds feed "stopped" ten days ago, so anything that reads
-- it is refused as stale instead of reporting a falsely low number (ADR 0010).
SELECT setseed(0.42);

INSERT INTO unit_a.orders (order_id, customer_id, customer_email, ordered_at, status, channel,
                           country, gross_amount, discount_amount, is_internal)
SELECT g,
       c.customer_id,
       'customer' || c.customer_id || '@example.com',
       (current_date - 1 - floor(random() * 400)::int) + random() * interval '86399 seconds',
       CASE WHEN random() < 0.03 THEN 'test' WHEN random() < 0.08 THEN 'cancelled' ELSE 'paid' END,
       (ARRAY['web', 'app', 'email'])[1 + floor(random() * 3)::int],
       (ARRAY['GB', 'DE', 'FR', 'US'])[1 + floor(random() * 4)::int],
       round((10 + random() * 190)::numeric, 2),
       CASE WHEN random() < 0.3 THEN round((random() * 20)::numeric, 2) ELSE 0 END,
       random() < 0.02
FROM generate_series(1, 20000) AS g
CROSS JOIN LATERAL (SELECT 1000 + floor(random() * 4000 + g * 0)::bigint AS customer_id) AS c;

INSERT INTO unit_b.orders (order_id, customer_id, customer_email, ordered_at, status, channel,
                           country, gross_amount, discount_amount, is_internal)
SELECT g,
       c.customer_id,
       'buyer' || c.customer_id || '@example.com',
       (current_date - 1 - floor(random() * 400)::int) + random() * interval '86399 seconds',
       CASE WHEN random() < 0.02 THEN 'test' WHEN random() < 0.05 THEN 'cancelled' ELSE 'paid' END,
       (ARRAY['marketplace', 'partner'])[1 + floor(random() * 2)::int],
       (ARRAY['GB', 'DE', 'FR', 'US'])[1 + floor(random() * 4)::int],
       round((5 + random() * 95)::numeric, 2),
       0,
       false
FROM generate_series(1, 12000) AS g
CROSS JOIN LATERAL (SELECT 1000 + floor(random() * 3000 + g * 0)::bigint AS customer_id) AS c;

UPDATE unit_a.orders o SET is_first_order = true
FROM (SELECT DISTINCT ON (customer_id) order_id FROM unit_a.orders ORDER BY customer_id, ordered_at) f
WHERE o.order_id = f.order_id;

UPDATE unit_b.orders o SET is_first_order = true
FROM (SELECT DISTINCT ON (customer_id) order_id FROM unit_b.orders ORDER BY customer_id, ordered_at) f
WHERE o.order_id = f.order_id;

INSERT INTO unit_a.refunds (refund_id, order_id, refunded_at, amount, reason)
SELECT row_number() OVER (), o.order_id, r.refunded_at, round((o.gross_amount * (0.3 + random() * 0.7))::numeric, 2),
       (ARRAY['damaged', 'late', 'changed_mind'])[1 + floor(random() * 3)::int]
FROM unit_a.orders o
CROSS JOIN LATERAL (SELECT o.ordered_at + random() * interval '30 days' AS refunded_at) r
WHERE o.status = 'paid' AND random() < 0.06 AND r.refunded_at < current_date;

INSERT INTO unit_b.refunds (refund_id, order_id, refunded_at, amount, reason)
SELECT row_number() OVER (), o.order_id, r.refunded_at, o.gross_amount,
       (ARRAY['not_received', 'not_as_described'])[1 + floor(random() * 2)::int]
FROM unit_b.orders o
CROSS JOIN LATERAL (SELECT o.ordered_at + random() * interval '30 days' AS refunded_at) r
WHERE o.status = 'paid' AND random() < 0.04 AND r.refunded_at < current_date - 10;

INSERT INTO unit_a.products (product_id, name, is_active, updated_at)
SELECT g, 'Product ' || g, random() < 0.85, now() - random() * interval '30 days'
FROM generate_series(1, 250) AS g;

ANALYZE;
