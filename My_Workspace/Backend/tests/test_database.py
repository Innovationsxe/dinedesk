import unittest

from Backend.database import _postgres_sql


class PostgresSqlTranslationTests(unittest.TestCase):
    def test_translates_sqlite_table_types_and_case_insensitive_email(self):
        sql, returning_id = _postgres_sql(
            """
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                price REAL NOT NULL
            )
            """
        )

        self.assertIn("id BIGSERIAL PRIMARY KEY", sql)
        self.assertIn("email TEXT NOT NULL", sql)
        self.assertNotIn("email TEXT NOT NULL UNIQUE", sql)
        self.assertIn("price DOUBLE PRECISION NOT NULL", sql)
        self.assertNotIn("COLLATE NOCASE", sql)
        self.assertFalse(returning_id)

    def test_translates_case_insensitive_queries_and_reservation_dates(self):
        sql, returning_id = _postgres_sql(
            """
            SELECT * FROM reservations
            WHERE email = ? COLLATE NOCASE
              AND datetime(reservation_at) < datetime(?)
              AND datetime(reservation_at, '+' || duration_minutes || ' minutes') > datetime(?)
            ORDER BY guest_name COLLATE NOCASE
            """
        )

        self.assertIn("LOWER(email) = LOWER(%s)", sql)
        self.assertIn("CAST(reservation_at AS TIMESTAMP) < CAST(%s AS TIMESTAMP)", sql)
        self.assertIn("duration_minutes * INTERVAL '1 minute'", sql)
        self.assertIn("LOWER(guest_name)", sql)
        self.assertFalse(returning_id)

    def test_translates_ignore_conflicts_and_returns_inserted_id(self):
        sql, returning_id = _postgres_sql(
            "INSERT OR IGNORE INTO staff_roster (email) SELECT email FROM users",
            returning_id=True,
        )

        self.assertEqual(
            sql,
            "INSERT INTO staff_roster (email) SELECT email FROM users "
            "ON CONFLICT DO NOTHING RETURNING id",
        )
        self.assertTrue(returning_id)

    def test_preserves_question_marks_inside_sql_strings(self):
        sql, _ = _postgres_sql("SELECT '?' AS label WHERE id = ?")
        self.assertEqual(sql, "SELECT '?' AS label WHERE id = %s")


if __name__ == "__main__":
    unittest.main()
