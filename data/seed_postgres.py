"""Seed the Supabase Postgres database with the same sample data the
SQLite seeder produced. Idempotent — every run drops and recreates the
schema before inserting.

Run:
    ./venv/bin/python data/seed_postgres.py

What it does:
  1. Executes data/schema.sql (drops + recreates all tables)
  2. Seeds 5 data tables: sales, customers, products, transactions,
     sales_rep_interactions
  3. Backfills customers.last_purchase_date + average_order_value from
     the transactions table (same semantics as the SQLite version)

The new RBAC tables (roles, users, agent_audit_log, outreach_drafts)
are CREATED here but seeded on Day 2 once role names are finalised.
"""

from __future__ import annotations

import os
import random
import sys
from datetime import date, timedelta

# Make the project root importable
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from db import connect  # noqa: E402

SCHEMA_PATH = os.path.join(_THIS_DIR, "schema.sql")


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
    return (date.today() - timedelta(days=random.randint(0, 365))).isoformat()


def _random_price(category: str) -> float:
    low, high = PRICE_RANGES[category]
    return round(random.uniform(low, high), 2)


def _make_email(first: str, last: str, i: int) -> str:
    return f"{first.lower()}.{last.lower()}{i}@example.com"


# ── Schema ─────────────────────────────────────────────────────────────
def _apply_schema(conn) -> None:
    with open(SCHEMA_PATH, "r") as f:
        sql = f.read()
    with conn.cursor() as cur:
        cur.execute(sql)


# ── Seeders ────────────────────────────────────────────────────────────
def _seed_sales(conn) -> None:
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
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO sales "
            "(id, date, product_name, category, quantity, unit_price, "
            "total_revenue, region, sales_rep) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )


def _seed_customers(conn) -> None:
    rows = []
    used_emails: set[str] = set()
    for i in range(1, 31):
        first = random.choice(FIRST_NAMES)
        last = random.choice(LAST_NAMES)
        name = f"{first} {last}"

        email = _make_email(first, last, i)
        while email in used_emails:
            email = _make_email(first, last, i + random.randint(1000, 9999))
        used_emails.add(email)

        region = random.choice(REGIONS)
        signup_date = _random_date_within_last_12_months()
        total_orders = random.randint(1, 40)
        total_spent = round(random.uniform(20.00, 5000.00), 2)
        status = random.choice(["active", "inactive", "vip"])

        rows.append(
            (i, name, email, region, signup_date,
             total_orders, total_spent, status, None, None)
        )

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO customers "
            "(id, name, email, region, signup_date, total_orders, "
            "total_spent, status, last_purchase_date, average_order_value) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            rows,
        )


def _seed_products(conn) -> None:
    rows = []
    for i in range(1, 21):
        category = random.choice(CATEGORIES)
        name = random.choice(PRODUCTS_BY_CATEGORY[category])
        unit_price = _random_price(category)
        stock_quantity = random.randint(0, 500)
        supplier = random.choice(SUPPLIERS)
        rows.append((i, name, category, unit_price, stock_quantity, supplier))

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO products "
            "(id, name, category, unit_price, stock_quantity, supplier) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )


def _seed_transactions(conn) -> None:
    """200 transactions; ~20% of customers are intentionally 'stale' so
    the scoring server has real churn-risk signal."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM customers")
        customer_ids = [r["id"] for r in cur.fetchall()]

    stale_ids = set(random.sample(customer_ids, k=max(3, len(customer_ids) // 5)))

    today = date.today()
    rows = []
    for i in range(1, 201):
        customer_id = random.choice(customer_ids)
        category = random.choice(CATEGORIES)
        product_name = random.choice(PRODUCTS_BY_CATEGORY[category])
        amount = _random_price(category) * random.randint(1, 5)

        if customer_id in stale_ids:
            delta_days = random.randint(100, 400)
        else:
            delta_days = int(random.triangular(0, 730, 60))

        txn_date = (today - timedelta(days=delta_days)).isoformat()
        rows.append((i, customer_id, txn_date, round(amount, 2), product_name, category))

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO transactions "
            "(id, customer_id, date, amount, product_name, category) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )


def _seed_sales_rep_interactions(conn) -> None:
    """80 rep-customer interactions. 60% successful / 30% unsuccessful / 10% pending."""
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM customers")
        customer_ids = [r["id"] for r in cur.fetchall()]

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

    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO sales_rep_interactions "
            "(id, customer_id, sales_rep, interaction_date, outcome, notes) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            rows,
        )


def _populate_customer_derived_columns(conn) -> None:
    """Fill last_purchase_date + average_order_value from transactions.

    Postgres equivalent of the SQLite correlated-subquery UPDATE.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE customers c
               SET last_purchase_date = sub.last_date,
                   average_order_value = sub.avg_amt
              FROM (
                    SELECT customer_id,
                           MAX(date) AS last_date,
                           ROUND(AVG(amount)::numeric, 2)::double precision AS avg_amt
                      FROM transactions
                     GROUP BY customer_id
                   ) AS sub
             WHERE c.id = sub.customer_id
            """
        )


# ── Entry point ────────────────────────────────────────────────────────
def main() -> None:
    random.seed()

    with connect() as conn:
        print("Applying schema (drop + recreate) ...")
        _apply_schema(conn)
        print("Seeding sales / customers / products / transactions / interactions ...")
        _seed_sales(conn)
        _seed_customers(conn)
        _seed_products(conn)
        _seed_transactions(conn)
        _seed_sales_rep_interactions(conn)
        _populate_customer_derived_columns(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM customers "
                "WHERE last_purchase_date IS NOT NULL"
            )
            with_txns = cur.fetchone()["c"]

    print("Done.")
    print("  sales:                  50 rows")
    print(f"  customers:              30 rows ({with_txns} have last_purchase_date)")
    print("  products:               20 rows")
    print("  transactions:          200 rows")
    print("  sales_rep_interactions: 80 rows")
    print("  roles / users / agent_audit_log / outreach_drafts: created (empty, seeded Day 2)")


if __name__ == "__main__":
    main()
