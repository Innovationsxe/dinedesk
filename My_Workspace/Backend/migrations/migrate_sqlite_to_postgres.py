import argparse
import os
import re
import sqlite3
import sys
from pathlib import Path


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
os.environ["BUNDU_SKIP_DATABASE_INIT"] = "1"

from app import DATABASE_ENGINE, DATABASE_PATH, get_connection, initialise_database


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BATCH_SIZE = 500


def quote_identifier(value):
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"Unexpected database identifier: {value!r}")
    return f'"{value}"'


def migrate(source_path):
    if DATABASE_ENGINE != "postgres":
        raise RuntimeError("Set BUNDU_DATABASE_URL to the destination PostgreSQL database.")
    if not source_path.is_file():
        raise FileNotFoundError(f"SQLite source database does not exist: {source_path}")

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    try:
        legacy_columns = {
            row["name"] for row in source.execute("PRAGMA table_info(orders)").fetchall()
        }
        if "payment_method" not in legacy_columns:
            raise RuntimeError(
                "The SQLite database has not completed the payment-method migration. "
                "Run Backend/migrations/migrate_payment_method.py first."
            )

        tables = [
            row["name"]
            for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]
        with get_connection() as target:
            existing_tables = target.execute(
                """
                SELECT COUNT(*) AS table_count
                FROM information_schema.tables
                WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'
                """
            ).fetchone()["table_count"]
            if existing_tables:
                raise RuntimeError(
                    "The PostgreSQL destination already contains tables. "
                    "Use a new, empty database to prevent accidental data changes."
                )

        initialise_database(seed_menu=False)

        with get_connection() as target:
            for table in tables:
                target_table = quote_identifier(table)
                count = target.execute(f"SELECT COUNT(*) AS count FROM {target_table}").fetchone()["count"]
                if count:
                    raise RuntimeError(
                        f"Destination table {table!r} is not empty. "
                        "Use a new PostgreSQL database to prevent accidental data merging."
                    )

            total_rows = 0
            for table in tables:
                quoted_table = quote_identifier(table)
                columns = [
                    row["name"]
                    for row in source.execute(f"PRAGMA table_info({quoted_table})").fetchall()
                ]
                if not columns:
                    continue
                quoted_columns = ", ".join(quote_identifier(column) for column in columns)
                placeholders = ", ".join("?" for _ in columns)
                insert_sql = (
                    f"INSERT INTO {quoted_table} ({quoted_columns}) "
                    f"VALUES ({placeholders})"
                )
                cursor = source.execute(f"SELECT {quoted_columns} FROM {quoted_table}")
                while True:
                    batch = cursor.fetchmany(BATCH_SIZE)
                    if not batch:
                        break
                    target.executemany(insert_sql, [tuple(row) for row in batch])
                    total_rows += len(batch)

                source_count = source.execute(
                    f"SELECT COUNT(*) AS count FROM {quoted_table}"
                ).fetchone()["count"]
                target_count = target.execute(
                    f"SELECT COUNT(*) AS count FROM {quoted_table}"
                ).fetchone()["count"]
                if source_count != target_count:
                    raise RuntimeError(
                        f"Record count mismatch for table {table!r}: "
                        f"SQLite has {source_count}, PostgreSQL has {target_count}."
                    )

                if "id" in columns:
                    target.execute(
                        f"""
                        SELECT setval(
                            pg_get_serial_sequence(?, 'id'),
                            COALESCE(MAX(id), 1),
                            MAX(id) IS NOT NULL
                        )
                        FROM {quoted_table}
                        """,
                        (table,),
                    )

        print(f"Migrated {total_rows} records from {source_path} to PostgreSQL.")
        print("Source SQLite database was left unchanged.")
    finally:
        source.close()


def main():
    parser = argparse.ArgumentParser(
        description="Copy the DineDesk SQLite database into an empty PostgreSQL database."
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(os.environ.get("BUNDU_SQLITE_PATH", DATABASE_PATH)),
        help="SQLite database to copy (defaults to BUNDU_SQLITE_PATH or Backend/complaints.db).",
    )
    args = parser.parse_args()
    migrate(args.source.resolve())


if __name__ == "__main__":
    main()
