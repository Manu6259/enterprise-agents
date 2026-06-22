"""Seed the analytics SQLite database with realistic sample data.

Idempotent — safe to run multiple times. Drops and recreates all tables
on every run, so no duplicate data ever accumulates.

Creates five tables:
  * sales                    — 50 rows across 12 months, 3 categories, 4 regions, 5 reps
  * customers                — 30 rows with status + last_purchase_date + avg order value
  * products                 — 20 rows across the same 3 categories
  * transactions             — 200 rows distributed across existing customers, 24-month span
  * sales_rep_interactions   — 80 rows of rep-customer interactions with outcomes

Launch:
    python data/seed_database.py
"""

from __future__ import annotations

import os
import random
import sqlite3
import sys
from datetime import date, timedelta

# Allow this standalone script to import config.py from the project root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from config import DATABASE_PATH  # noqa: E402


# ── Data catalogues ────────────────────────────────────────────────────
CATEGORIES = ["Electronics", "Clothing", "Food"]
REGIONS = ["North", "South", "East", "West"]
SALES_REPS = ["Alice", "Bob", "Carol", "David", "Emma"]

PRODUCTS_BY_CATEGORY = {
    "Electronics": ["Laptop", "Headphones", "Keyboard", "Monitor", "Webcam", "Mouse"],
    "Clothing": ["T-Shirt", "Jeans", "Jacket", "Sneakers", "Dress", "Hoodie"],
    "Food": ["Coffee Beans", "Olive Oil", "Pasta", "Granola", "Hot Sauce", "Tea"],
}

PRICE_RANGES = {
    "Electronics": (50.00, 999.00),
    "Clothing": (15.00, 120.00),
    "Food": (2.00, 25.00),
}

FIRST_NAMES = [
    "James", "Mary", "Robert", "Patricia", "John", "Jennifer", "Michael",
    "Linda", "William", "Elizabeth", "David", "Barbara", "Richard", "Susan",
    "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen", "Christopher",
    "Nancy", "Daniel", "Lisa", "Matthew", "Betty", "Anthony", "Helen",
    "Donald", "Sandra",
]
LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark",
    "Ramirez", "Lewis", "Robinson",
]

SUPPLIERS = [
    "Acme Corp", "Globex Industries", "Initech", "Umbrella Trading",
    "Stark Supplies", "Wayne Distribution", "Pied Piper", "Hooli Goods",
    "Wonka Wholesale", "Cyberdyne Systems",
]


# ── Helpers ────────────────────────────────────────────────────────────
def _random_date_within_last_12_months() -> str:
    today = date.today()
    delta_days = random.randint(0, 365)
    return (today - timedelta(days=delta_days)).isoformat()


def _random_date_within_last_24_months() -> str:
    today = date.today()
    delta_days = random.randint(0, 730)
    return (today - timedelta(days=delta_days)).isoformat()


def _random_price(category: str) -> float:
    low, high = PRICE_RANGES[category]
    return round(random.uniform(low, high), 2)


def _make_email(first: str, last: str, i: int) -> str:
    # Append the index to guarantee uniqueness across the 30 customers
    return f"{first.lower()}.{last.lower()}{i}@example.com"


# ── Table creation ─────────────────────────────────────────────────────
def _create_tables(conn: sqlite3.Connection) -> None:
    cursor = conn.cursor()
    cursor.executescript(
        """
        DROP TABLE IF EXISTS sales;
        DROP TABLE IF EXISTS customers;
        DROP TABLE IF EXISTS products;
        DROP TABLE IF EXISTS transactions;
        DROP TABLE IF EXISTS sales_rep_interactions;

        CREATE TABLE sales (
            id             INTEGER PRIMARY KEY,
            date           TEXT,
            product_name   TEXT,
            category       TEXT,
            quantity       INTEGER,
            unit_price     REAL,
            total_revenue  REAL,
            region         TEXT,
            sales_rep      TEXT
        );

        CREATE TABLE customers (
            id                    INTEGER PRIMARY KEY,
            name                  TEXT,
            email                 TEXT,
            region                TEXT,
            signup_date           TEXT,
            total_orders          INTEGER,
            total_spent           REAL,
            status                TEXT,
            last_purchase_date    TEXT,
            average_order_value   REAL
        );

        CREATE TABLE products (
            id              INTEGER PRIMARY KEY,
            name            TEXT,
            category        TEXT,
            unit_price      REAL,
            stock_quantity  INTEGER,
            supplier        TEXT
        );

        CREATE TABLE transactions (
            id            INTEGER PRIMARY KEY,
            customer_id   INTEGER,
            date          TEXT,
            amount        REAL,
            product_name  TEXT,
            category      TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers (id)
        );

        CREATE TABLE sales_rep_interactions (
            id                INTEGER PRIMARY KEY,
            customer_id       INTEGER,
            sales_rep         TEXT,
            interaction_date  TEXT,
            outcome           TEXT,
            notes             TEXT,
            FOREIGN KEY (customer_id) REFERENCES customers (id)
        );
        """
    )


def _seed_sales(conn: sqlite3.Connection) -> None:
    rows = []
    for i in range(1, 51):
        category = random.choice(CATEGORIES)
        product_name = random.choice(PRODUCTS_BY_CATEGORY[category])
        quantity = random.randint(1, 50)
        unit_price = _random_price(category)
        total_revenue = round(quantity * unit_price, 2)
        rows.append(
            (
                i,
                _random_date_within_last_12_months(),
                product_name,
                category,
                quantity,
                unit_price,
                total_revenue,
                random.choice(REGIONS),
                random.choice(SALES_REPS),
            )
        )
    conn.executemany(
        "INSERT INTO sales "
        "(id, date, product_name, category, quantity, unit_price, "
        "total_revenue, region, sales_rep) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _seed_customers(conn: sqlite3.Connection) -> None:
    rows = []
    used_emails: set[str] = set()
    for i in range(1, 31):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        name = f"{first} {last}"

        # Guarantee unique email per row
        email = _make_email(first, last, i)
        while email in used_emails:
            email = _make_email(first, last, i + random.randint(1000, 9999))
        used_emails.add(email)

        region = random.choice(REGIONS)
        signup_date = _random_date_within_last_12_months()
        total_orders = random.randint(1, 40)
        total_spent = round(random.uniform(20.00, 5000.00), 2)
        status = random.choice(["active", "inactive", "vip"])

        # last_purchase_date + average_order_value are populated later
        # from the transactions table.
        rows.append(
            (
                i, name, email, region, signup_date,
                total_orders, total_spent, status,
                None, None,
            )
        )

    conn.executemany(
        "INSERT INTO customers "
        "(id, name, email, region, signup_date, total_orders, "
        "total_spent, status, last_purchase_date, average_order_value) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )


def _seed_products(conn: sqlite3.Connection) -> None:
    rows = []
    for i in range(1, 21):
        category = random.choice(CATEGORIES)
        name = random.choice(PRODUCTS_BY_CATEGORY[category])
        unit_price = _random_price(category)
        stock_quantity = random.randint(0, 500)
        supplier = random.choice(SUPPLIERS)
        rows.append((i, name, category, unit_price, stock_quantity, supplier))

    conn.executemany(
        "INSERT INTO products "
        "(id, name, category, unit_price, stock_quantity, supplier) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


# ── Transactions (new) ─────────────────────────────────────────────────
def _seed_transactions(conn: sqlite3.Connection) -> None:
    """Insert 200 transactions distributed across existing customers.

    Some customers are given recent activity, some are intentionally
    stale (>90 days since last purchase) so the scoring server has
    real churn-risk signal to work with.
    """
    customer_rows = conn.execute("SELECT id FROM customers").fetchall()
    customer_ids = [r[0] for r in customer_rows]

    # Designate a handful of customers as "stale" — their last
    # transaction is intentionally pushed into the past by 100-400 days.
    stale_ids = set(random.sample(customer_ids, k=max(3, len(customer_ids) // 5)))

    today = date.today()
    rows = []
    for i in range(1, 201):
        customer_id = random.choice(customer_ids)
        category = random.choice(CATEGORIES)
        product_name = random.choice(PRODUCTS_BY_CATEGORY[category])
        amount = _random_price(category) * random.randint(1, 5)

        if customer_id in stale_ids:
            # Push this transaction into the far past
            delta_days = random.randint(100, 400)
        else:
            # Spread across last 24 months, biased toward recent
            delta_days = int(random.triangular(0, 730, 60))

        txn_date = (today - timedelta(days=delta_days)).isoformat()

        rows.append((i, customer_id, txn_date, round(amount, 2), product_name, category))

    conn.executemany(
        "INSERT INTO transactions "
        "(id, customer_id, date, amount, product_name, category) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _seed_sales_rep_interactions(conn: sqlite3.Connection) -> None:
    """Insert 80 rep-customer interactions.

    Outcome distribution: 60% successful, 30% unsuccessful, 10% pending.
    Dates spread across the last 12 months. Each interaction picks from
    the existing customers and the existing sales rep roster.
    """
    customer_rows = conn.execute("SELECT id FROM customers").fetchall()
    customer_ids = [r[0] for r in customer_rows]

    outcome_population = (
        ["successful"] * 60 + ["unsuccessful"] * 30 + ["pending"] * 10
    )

    note_templates = {
        "successful": [
            "Closed upsell — added premium item.",
            "Customer signed renewal early.",
            "Converted from trial to paid.",
            "Positive call — exploring larger order next quarter.",
        ],
        "unsuccessful": [
            "Customer went silent after demo.",
            "Budget cut — deferred indefinitely.",
            "Chose a competitor product.",
            "Didn't match requirements.",
        ],
        "pending": [
            "Waiting on procurement decision.",
            "Follow-up scheduled for next week.",
            "Proposal under internal review.",
            "Reminder set — customer traveling.",
        ],
    }

    rows = []
    for i in range(1, 81):
        customer_id = random.choice(customer_ids)
        sales_rep = random.choice(SALES_REPS)
        interaction_date = _random_date_within_last_12_months()
        outcome = random.choice(outcome_population)
        notes = random.choice(note_templates[outcome])
        rows.append((i, customer_id, sales_rep, interaction_date, outcome, notes))

    conn.executemany(
        "INSERT INTO sales_rep_interactions "
        "(id, customer_id, sales_rep, interaction_date, outcome, notes) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )


def _populate_customer_derived_columns(conn: sqlite3.Connection) -> None:
    """Fill last_purchase_date + average_order_value from transactions."""
    conn.execute(
        """
        UPDATE customers
           SET last_purchase_date = (
                 SELECT MAX(date) FROM transactions
                  WHERE transactions.customer_id = customers.id
               ),
               average_order_value = (
                 SELECT ROUND(AVG(amount), 2) FROM transactions
                  WHERE transactions.customer_id = customers.id
               )
        """
    )


# ── Entry point ────────────────────────────────────────────────────────
def main() -> None:
    # Seed random for reasonable stability across runs but not identical
    random.seed()

    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

    with sqlite3.connect(DATABASE_PATH) as conn:
        _create_tables(conn)
        _seed_sales(conn)
        _seed_customers(conn)
        _seed_products(conn)
        _seed_transactions(conn)
        _seed_sales_rep_interactions(conn)
        _populate_customer_derived_columns(conn)
        conn.commit()

        with_txns = conn.execute(
            "SELECT COUNT(*) FROM customers WHERE last_purchase_date IS NOT NULL"
        ).fetchone()[0]

    print(f"Seeded database at: {DATABASE_PATH}")
    print("  sales:                  50 rows")
    print("  customers:              30 rows "
          f"({with_txns} now have last_purchase_date populated)")
    print("  products:               20 rows")
    print("  transactions:          200 rows")
    print("  sales_rep_interactions: 80 rows")


if __name__ == "__main__":
    main()
