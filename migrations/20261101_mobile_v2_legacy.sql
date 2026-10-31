-- Additive migration for the legacy SQLite runtime. Safe to run repeatedly.
CREATE TABLE IF NOT EXISTS mobile_v2_orders (
    user_id INTEGER NOT NULL,
    request_id TEXT NOT NULL,
    plan_code TEXT NOT NULL,
    provider TEXT NOT NULL,
    amount_rub INTEGER NOT NULL,
    days INTEGER NOT NULL,
    devices INTEGER NOT NULL,
    payment_id TEXT,
    payment_url TEXT,
    provider_payload TEXT,
    state TEXT NOT NULL DEFAULT 'creating',
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY(user_id, request_id),
    UNIQUE(provider, payment_id)
);
CREATE INDEX IF NOT EXISTS idx_mobile_v2_orders_invoice
    ON mobile_v2_orders(provider, payment_id);
