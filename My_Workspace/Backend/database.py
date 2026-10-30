import re
import sqlite3


DATABASE_URL = ""
DATABASE_ENGINE = "sqlite"
POSTGRES_WRITE_LOCK_ID = 0x44494E454445534B


def configure_database(database_url):
    global DATABASE_URL, DATABASE_ENGINE
    DATABASE_URL = (database_url or "").strip()
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
    DATABASE_ENGINE = "postgres" if DATABASE_URL else "sqlite"


def get_connection(sqlite_path):
    if DATABASE_ENGINE == "sqlite":
        connection = sqlite3.connect(sqlite_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise RuntimeError(
            "PostgreSQL is configured but psycopg is not installed. "
            "Install Backend/requirements.txt."
        ) from error

    connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)
    return PostgresConnection(connection)


def get_integrity_errors():
    if DATABASE_ENGINE == "postgres":
        import psycopg

        return (psycopg.IntegrityError,)
    return (sqlite3.IntegrityError,)


def get_database_errors():
    if DATABASE_ENGINE == "postgres":
        import psycopg

        return (psycopg.Error,)
    return (sqlite3.Error,)


def get_table_columns(connection, table):
    if DATABASE_ENGINE == "postgres":
        rows = connection.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = ?
            """,
            (table,),
        ).fetchall()
    else:
        rows = connection.execute(f"PRAGMA table_info({table})").fetchall()
    return {row["name"] for row in rows}


class PostgresConnection:
    def __init__(self, connection):
        self._connection = connection

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._connection.__exit__(exc_type, exc_value, traceback)

    def execute(self, query, parameters=()):
        if re.fullmatch(r"\s*BEGIN\s+IMMEDIATE\s*;?\s*", query, flags=re.IGNORECASE):
            cursor = self._connection.cursor()
            cursor.execute("BEGIN")
            cursor.execute(
                "SELECT pg_advisory_xact_lock(%s)",
                (POSTGRES_WRITE_LOCK_ID,),
            )
            return PostgresCursor(cursor)
        sql, returning_id = _postgres_sql(query, returning_id=True)
        cursor = self._connection.cursor()
        cursor.execute(sql, parameters)
        return PostgresCursor(cursor, returning_id)

    def executemany(self, query, parameters):
        sql, _ = _postgres_sql(query, returning_id=False)
        cursor = self._connection.cursor()
        cursor.executemany(sql, parameters)
        return PostgresCursor(cursor)


class PostgresCursor:
    def __init__(self, cursor, returning_id=False):
        self._cursor = cursor
        self._returning_id = returning_id
        self._lastrowid = None
        self._returning_consumed = False

    def _consume_returned_id(self):
        if self._returning_id and not self._returning_consumed:
            row = self._cursor.fetchone()
            self._lastrowid = row["id"] if row else None
            self._returning_consumed = True

    @property
    def lastrowid(self):
        self._consume_returned_id()
        return self._lastrowid

    @property
    def rowcount(self):
        return self._cursor.rowcount

    def fetchone(self):
        self._consume_returned_id()
        return self._cursor.fetchone()

    def fetchall(self):
        self._consume_returned_id()
        return self._cursor.fetchall()


def _postgres_sql(query, returning_id=False):
    sql = query.strip().rstrip(";")
    if re.fullmatch(r"BEGIN\s+IMMEDIATE", sql, flags=re.IGNORECASE):
        return "BEGIN", False

    ignore_conflicts = bool(
        re.match(r"INSERT\s+OR\s+IGNORE\s+INTO\b", sql, flags=re.IGNORECASE)
    )
    if ignore_conflicts:
        sql = re.sub(
            r"INSERT\s+OR\s+IGNORE\s+INTO\b",
            "INSERT INTO",
            sql,
            count=1,
            flags=re.IGNORECASE,
        )

    sql = re.sub(
        r"datetime\(\s*([A-Za-z_][\w.]*)\s*,\s*'\+'\s*\|\|\s*"
        r"([A-Za-z_][\w.]*)\s*\|\|\s*' minutes'\s*\)",
        r"(CAST(\1 AS TIMESTAMP) + \2 * INTERVAL '1 minute')",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"datetime\(\s*([A-Za-z_][\w.]*)\s*\)",
        r"CAST(\1 AS TIMESTAMP)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"datetime\(\s*(\?)\s*\)", r"CAST(\1 AS TIMESTAMP)", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"([A-Za-z_][\w.]*)\s*=\s*(\?)\s+COLLATE\s+NOCASE",
        r"LOWER(\1) = LOWER(\2)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"\bemail\s+TEXT\s+NOT\s+NULL\s+UNIQUE\s+COLLATE\s+NOCASE\b",
        "email TEXT NOT NULL",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(
        r"([A-Za-z_][\w.]*)\s+COLLATE\s+NOCASE",
        r"LOWER(\1)",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\bCOLLATE\s+NOCASE\b", "", sql, flags=re.IGNORECASE)
    sql = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        sql,
        flags=re.IGNORECASE,
    )
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.IGNORECASE)

    if ignore_conflicts:
        sql += " ON CONFLICT DO NOTHING"

    is_insert = bool(re.match(r"INSERT\s+INTO\b", sql, flags=re.IGNORECASE))
    if returning_id and is_insert and not re.search(r"\bRETURNING\b", sql, flags=re.IGNORECASE):
        sql += " RETURNING id"

    return _replace_placeholders(sql), returning_id and is_insert


def _replace_placeholders(sql):
    output = []
    quote = None
    index = 0
    while index < len(sql):
        character = sql[index]
        if quote:
            output.append(character)
            if character == quote:
                if index + 1 < len(sql) and sql[index + 1] == quote:
                    output.append(sql[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            output.append(character)
        elif character == "?":
            output.append("%s")
        else:
            output.append(character)
        index += 1
    return "".join(output)
