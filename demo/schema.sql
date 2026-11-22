CREATE TABLE customers (
    id TEXT PRIMARY KEY NOT NULL,
    name TEXT NOT NULL
);
CREATE TABLE carts (
    id TEXT PRIMARY KEY NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    sku TEXT NOT NULL CHECK (sku = 'cloud-mug')
);
CREATE TABLE orders (
    id TEXT PRIMARY KEY NOT NULL,
    customer_id TEXT NOT NULL REFERENCES customers(id),
    cart_id TEXT NOT NULL UNIQUE,
    intention TEXT NOT NULL UNIQUE,
    amount_cents INTEGER NOT NULL CHECK (amount_cents = 2500),
    receipt TEXT NOT NULL UNIQUE
);
