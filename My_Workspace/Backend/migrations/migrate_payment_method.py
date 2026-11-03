"""Safely migrate orders.bill_plan to orders.payment_method.

Preview the proposed migration:
    .venv\\Scripts\\python.exe Backend\\migrations\\migrate_payment_method.py

After stopping the backend and approving the legacy-value mapping, apply it:
    .venv\\Scripts\\python.exe Backend\\migrations\\migrate_payment_method.py --apply

The script creates a SQLite backup before rebuilding the orders table. It maps
pay_individual to cash and split_bill to card, and refuses unknown values or
unexpected table objects instead of guessing.
"""

import argparse
from datetime import datetime
from pathlib import Path
import sqlite3
import sys


BACKEND_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = BACKEND_DIR / "complaints.db"
EXPECTED_COLUMNS = {
    "id",
    "customer_name",
    "customer_email",
    "items",
    "total",
    "status",
    "created_at",
    "updated_at",
    "order_number",
    "waiter_id",
    "order_type",
    "table_number",
    "bill_plan",
    "rating",
    "tracking_token_hash",
}
LEGACY_MAPPING = {
    "pay_individual": "cash",
    "split_bill": "card",
}


def inspect_database(database_path: Path) -> tuple[sqlite3.Connection, list[tuple[str, int]]]:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(orders)").fetchall()
    }
    if "payment_method" in columns and "bill_plan" not in columns:
        return connection, []
    if columns != EXPECTED_COLUMNS:
        connection.close()
        raise RuntimeError(
            "Unexpected orders table schema; refusing to migrate. "
            f"Found columns: {sorted(columns)}"
        )

    foreign_keys = connection.execute("PRAGMA foreign_key_list(orders)").fetchall()
    table_objects = connection.execute(
        "SELECT type, name FROM sqlite_master "
        "WHERE tbl_name = 'orders' AND type IN ('index', 'trigger') "
        "AND name NOT LIKE 'sqlite_autoindex_%'"
    ).fetchall()
    if foreign_keys or table_objects:
        connection.close()
        raise RuntimeError(
            "The orders table has foreign keys, custom indexes, or triggers. "
            "Review those dependencies before migrating."
        )

    counts = [
        (row["bill_plan"], row["count"])
        for row in connection.execute(
            "SELECT bill_plan, COUNT(*) AS count FROM orders "
            "GROUP BY bill_plan ORDER BY bill_plan"
        )
    ]
    unsupported = [value for value, _ in counts if value not in LEGACY_MAPPING]
    if unsupported:
        connection.close()
        raise RuntimeError(
            f"Unknown legacy payment values {unsupported}; refusing to guess their mapping."
        )
    return connection, counts


def migrate(database_path: Path) -> Path | None:
    connection, counts = inspect_database(database_path)
    if not counts:
        connection.close()
        return None

    backup_path = database_path.with_name(
        f"{database_path.stem}.pre-payment-method-{datetime.now():%Y%m%d-%H%M%S}.db"
    )
    if backup_path.exists():
        connection.close()
        raise FileExistsError(f"Refusing to overwrite backup: {backup_path}")

    try:
        with sqlite3.connect(backup_path) as backup:
            connection.backup(backup)

        connection.execute("BEGIN IMMEDIATE")
        sequence_row = connection.execute(
            "SELECT seq FROM sqlite_sequence WHERE name = 'orders'"
        ).fetchone()
        previous_sequence = sequence_row["seq"] if sequence_row else 0
        connection.execute(
            """
            CREATE TABLE orders_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                items TEXT NOT NULL,
                total REAL NOT NULL CHECK (total >= 0),
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                order_number TEXT,
                waiter_id INTEGER,
                order_type TEXT NOT NULL DEFAULT 'Dine in',
                table_number INTEGER,
                payment_method TEXT NOT NULL DEFAULT 'cash'
                    CHECK (payment_method IN ('cash', 'card')),
                rating INTEGER,
                tracking_token_hash TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO orders_new (
                id, customer_name, customer_email, items, total, status,
                created_at, updated_at, order_number, waiter_id, order_type,
                table_number, payment_method, rating, tracking_token_hash
            )
            SELECT
                id, customer_name, customer_email, items, total, status,
                created_at, updated_at, order_number, waiter_id, order_type,
                table_number,
                CASE bill_plan
                    WHEN 'pay_individual' THEN 'cash'
                    WHEN 'split_bill' THEN 'card'
                END,
                rating, tracking_token_hash
            FROM orders
            """
        )
        connection.execute("DROP TABLE orders")
        connection.execute("ALTER TABLE orders_new RENAME TO orders")
        connection.execute(
            "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'orders'",
            (previous_sequence,),
        )
        if connection.execute(
            "SELECT 1 FROM sqlite_sequence WHERE name = 'orders'"
        ).fetchone() is None:
            connection.execute(
                "INSERT INTO sqlite_sequence (name, seq) VALUES ('orders', ?)",
                (previous_sequence,),
            )
        connection.commit()
    except Exception:
        connection.rollback()
        connection.close()
        if backup_path.exists():
            backup_path.unlink()
        raise
    connection.close()
    return backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help=f"SQLite database path (default: {DEFAULT_DATABASE})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Create a backup and apply the table/data migration.",
    )
    args = parser.parse_args()

    if not args.database.is_file():
        parser.error(f"Database does not exist: {args.database}")

    connection, counts = inspect_database(args.database)
    connection.close()
    if not counts:
        print("Database already uses payment_method; no migration is needed.")
        return 0

    print("Proposed existing-order mapping:")
    for old_value, count in counts:
        print(f"  {old_value} -> {LEGACY_MAPPING[old_value]} ({count} orders)")
    if not args.apply:
        print("Read-only preview only; no database changes were made.")
        print("Stop the backend and add --apply only after approving this mapping.")
        return 0

    backup = migrate(args.database)
    print(f"Migration complete. Backup: {backup}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, sqlite3.Error, RuntimeError) as error:
        print(f"Migration aborted: {error}", file=sys.stderr)
        raise SystemExit(1) from error
