# Database backends

DineDesk continues to use SQLite by default. To enable PostgreSQL, set
`BUNDU_DATABASE_URL` to the PostgreSQL connection URL in the backend environment.
The backend then creates/updates its PostgreSQL schema during startup. The
destination database must be reachable and allow the app user to create tables,
and indexes.

## Migrating existing records

1. Stop the backend and make a backup of `Backend/complaints.db`.
2. Create a new, empty PostgreSQL database and set `BUNDU_DATABASE_URL`.
3. Install backend requirements, including `psycopg`:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r Backend\requirements.txt
   ```
4. Run the migration from the workspace root:

   ```powershell
   $env:BUNDU_DATABASE_URL = "postgresql://..."
   .\.venv\Scripts\python.exe Backend\migrations\migrate_sqlite_to_postgres.py
   ```

   Set `BUNDU_SQLITE_PATH` or pass `--source` if the SQLite database is not at
   `Backend/complaints.db`.
5. Start the backend with the same PostgreSQL URL and verify sign-in, ordering,
   staff updates, reservations, and reporting before directing customers to it.

The migration refuses a non-empty PostgreSQL destination and leaves the SQLite
source unchanged. Keep the SQLite backup until the PostgreSQL deployment has
been verified. Removing `BUNDU_DATABASE_URL` switches back to SQLite; records
created in PostgreSQL after cutover will not appear in SQLite.
