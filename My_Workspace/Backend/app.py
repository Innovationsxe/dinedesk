from datetime import date, datetime, time, timedelta, timezone
import csv
from email.message import EmailMessage
from html import escape
import hashlib
import hmac
import math
from functools import wraps
import json
import os
from pathlib import Path
import re
import secrets
import smtplib
import ssl
import textwrap
import uuid
from urllib.parse import urlsplit
import jwt
from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

if __package__:
    from .database import (
        configure_database,
        get_connection as open_database_connection,
        get_database_errors,
        get_integrity_errors,
        get_table_columns,
    )
else:
    from database import (
        configure_database,
        get_connection as open_database_connection,
        get_database_errors,
        get_integrity_errors,
        get_table_columns,
    )

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

APP_ENV = os.environ.get("BUNDU_ENV", "production").strip().lower()
if APP_ENV not in {"development", "production"}:
    raise RuntimeError("BUNDU_ENV must be either 'development' or 'production'")
IS_DEVELOPMENT = APP_ENV == "development"

DATABASE_PATH = Path(os.environ.get("BUNDU_DATABASE_PATH", BASE_DIR / "complaints.db"))
configure_database(os.environ.get("BUNDU_DATABASE_URL"))
DATABASE_ENGINE = "postgres" if os.environ.get("BUNDU_DATABASE_URL", "").strip() else "sqlite"
DATABASE_INTEGRITY_ERRORS = get_integrity_errors()
DATABASE_ERRORS = get_database_errors()
MENU_DATA_PATH = BASE_DIR / "sushi_order_items_clean.csv"
SIGNATURE_MENU_DATA_PATH = BASE_DIR / "signature_menu.csv"
FRONTEND_DIST_PATH = BASE_DIR.parent / "FrontEnd" / "dist"
UPLOAD_FOLDER = BASE_DIR / "uploads"
JWT_SECRET = os.environ.get("BUNDU_JWT_SECRET", "")
if len(JWT_SECRET.encode("utf-8")) < 32:
    raise RuntimeError("BUNDU_JWT_SECRET must be set to a random value of at least 32 bytes")
MANAGER_REGISTRATION_KEY = os.environ.get("BUNDU_MANAGER_REGISTRATION_KEY", "")
if not IS_DEVELOPMENT and len(MANAGER_REGISTRATION_KEY.encode("utf-8")) < 32:
    raise RuntimeError(
        "BUNDU_MANAGER_REGISTRATION_KEY must be set to a random value of at least 32 bytes"
    )
WAITER_REGISTRATION_KEY = os.environ.get("BUNDU_WAITER_REGISTRATION_KEY", "")
if not IS_DEVELOPMENT and len(WAITER_REGISTRATION_KEY.encode("utf-8")) < 32:
    raise RuntimeError(
        "BUNDU_WAITER_REGISTRATION_KEY must be set to a random value of at least 32 bytes"
    )
ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_IMAGE_SIZE = 5 * 1024 * 1024
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SMTP_TIMEOUT_SECONDS = 15
EMAIL_SEND_STALE_SECONDS = 120

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_IMAGE_SIZE + 1024 * 1024
CORS_ORIGINS = [
    origin.strip()
    for origin in os.environ.get(
        "BUNDU_ALLOWED_ORIGINS",
        "http://127.0.0.1:5500,http://localhost:5500",
    ).split(",")
    if origin.strip()
]
if not CORS_ORIGINS:
    raise RuntimeError("BUNDU_ALLOWED_ORIGINS must contain at least one exact origin")
for origin in CORS_ORIGINS:
    parsed_origin = urlsplit(origin)
    if (
        origin == "*"
        or parsed_origin.scheme not in {"http", "https"}
        or not parsed_origin.netloc
        or parsed_origin.path
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.username
        or parsed_origin.password
        or (
            parsed_origin.scheme == "http"
            and parsed_origin.hostname not in {"localhost", "127.0.0.1", "::1"}
        )
    ):
        raise RuntimeError(
            "BUNDU_ALLOWED_ORIGINS must contain exact HTTPS origins "
            "(HTTP is permitted only for localhost)"
        )
CORS(app, resources={r"/api/*": {"origins": CORS_ORIGINS}})
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per minute"],
    storage_uri=os.environ.get("BUNDU_RATE_LIMIT_STORAGE_URI", "memory://"),
)


@app.errorhandler(429)
def handle_rate_limit_error(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Too many requests. Please wait before trying again."}), 429
    return error


@app.after_request
def add_security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    response.headers.setdefault("X-Permitted-Cross-Domain-Policies", "none")
    if request.path.startswith("/api/"):
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'none'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'",
        )
        response.headers["Cache-Control"] = "no-store"
    elif response.mimetype == "text/html":
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; img-src 'self' data: https:; "
            "connect-src 'self'; object-src 'none'; base-uri 'self'; "
            "form-action 'self'; frame-ancestors 'none'",
        )
    if request.is_secure:
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response

VALID_STATUSES = {"Open", "Under Review", "Waiting for Customer", "Resolved"}
VALID_CATEGORIES = {"Wrong order", "Missing item", "Food quality", "Food temperature", "Long waiting time", "Poor service", "Incorrect bill", "Payment problem", "Hygiene concern", "Other"}
VALID_SEVERITIES = {"General feedback", "Problem with my order", "Urgent issue"}
VALID_ORDER_STATUSES = {
    "Received", "Preparing", "Ready", "Served", "Paid", "Completed", "Cancelled"
}
VALID_ORDER_TYPES = {"Dine in", "Takeaway", "Delivery"}
TABLE_NUMBERS = set(chr(i) for i in range(ord('A'), ord('Z') + 1))
VALID_ROLES = {"waiter", "manager"}
STAFF_ROSTER_ROLES = {"chef", "waiter", "manager"}
SOUTH_AFRICA_TIMEZONE = timezone(timedelta(hours=2))
STAFF_HOURLY_RATES = {"waiter": 35.0, "manager": 55.0}
WEEKLY_OVERTIME_THRESHOLD_HOURS = 45.0
OVERTIME_MULTIPLIER = 1.5
RESERVATION_DURATION_MINUTES = 60
RESERVATION_FEE = 50.0
PANTRY_CATEGORIES = {
    "Produce", "Meat", "Dairy", "Dry Goods", "Beverages", "Spices", "Cleaning/Other"
}
PANTRY_UNITS = {"kg", "g", "L", "ml", "pcs", "box"}
PANTRY_MOVEMENT_REASONS = {"Usage", "Wastage", "Spoilage"}

MENU_PRESENTATION = {
    "roll": ("🍣", "Freshly rolled favourites made with seasoned rice and quality fillings."),
    "nigiri": ("🍣", "Hand-shaped sushi rice topped with fresh, carefully prepared fish."),
    "side": ("🥢", "A comforting side to complete your DineDesk dining experience."),
    "drink": ("🥤", "A refreshing drink selected to pair beautifully with your meal."),
}


def get_connection():
    return open_database_connection(DATABASE_PATH)


def load_menu_items_from_csv():
    menu_items = {}
    with MENU_DATA_PATH.open(newline="", encoding="utf-8") as menu_file:
        for row in csv.DictReader(menu_file):
            name = row["item"].strip()
            category = row["category"].strip().lower()
            icon, description = MENU_PRESENTATION.get(
                category,
                ("🍽", "A DineDesk kitchen favourite, prepared fresh to order."),
            )
            menu_items[name] = (
                name,
                category.title(),
                description,
                float(row["unit_price"]),
                icon,
            )
    return list(menu_items.values())


def load_signature_menu_from_csv():
    items = []
    with SIGNATURE_MENU_DATA_PATH.open(newline="", encoding="utf-8") as menu_file:
        for row in csv.DictReader(menu_file):
            items.append((
                row["item"].strip(),
                row["category"].strip(),
                row["description"].strip(),
                float(row["price"]),
                row["icon"].strip(),
                row["image"].strip(),
            ))
    return items


def initialise_database(seed_menu=True):
    with get_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                firstname TEXT NOT NULL,
                lastname TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('waiter', 'manager')),
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        if DATABASE_ENGINE == "postgres":
            connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email_lower "
                "ON users(LOWER(email))"
            )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS menu_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                description TEXT NOT NULL,
                price REAL NOT NULL CHECK (price >= 0),
                icon TEXT NOT NULL,
                is_public INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS promotions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                menu_item_id INTEGER NOT NULL,
                discount_percent INTEGER NOT NULL,
                start_at TEXT NOT NULL,
                end_at TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_name TEXT NOT NULL,
                role TEXT NOT NULL,
                shift_label TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS attendance_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                session_id TEXT NOT NULL UNIQUE,
                clock_in_at TEXT NOT NULL,
                clock_out_at TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS roster_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                shift_label TEXT NOT NULL,
                scheduled_start TEXT NOT NULL,
                scheduled_end TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_roster (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                firstname TEXT NOT NULL,
                lastname TEXT NOT NULL,
                email TEXT UNIQUE,
                role TEXT NOT NULL CHECK (role IN ('chef', 'waiter', 'manager')),
                hourly_rate REAL NOT NULL CHECK (hourly_rate >= 0),
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_roster_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_id INTEGER NOT NULL,
                work_date TEXT NOT NULL,
                shift_label TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (staff_id, work_date, shift_label)
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS staff_password_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                staff_id INTEGER,
                user_id INTEGER,
                staff_name TEXT NOT NULL,
                email TEXT NOT NULL,
                generated_by INTEGER NOT NULL,
                generated_by_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                used_at TEXT,
                status TEXT NOT NULL DEFAULT 'current'
                    CHECK (status IN ('current', 'used', 'replaced', 'revoked'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS order_table_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER NOT NULL,
                table_number TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                fee_amount REAL NOT NULL DEFAULT 50.0,
                fee_paid INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'Booked',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_name TEXT NOT NULL,
                guest_email TEXT NOT NULL DEFAULT '',
                guest_phone TEXT NOT NULL DEFAULT '',
                party_size INTEGER NOT NULL DEFAULT 1,
                table_number TEXT NOT NULL,
                reservation_at TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL DEFAULT 90,
                status TEXT NOT NULL DEFAULT 'Confirmed',
                channel TEXT NOT NULL DEFAULT 'website',
                special_requests TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS waitlist (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guest_name TEXT NOT NULL,
                guest_phone TEXT NOT NULL,
                party_size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'Waiting',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_order_reservations_table_time ON order_table_reservations(table_number, starts_at, ends_at, status)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_attendance_clock_in ON attendance_records(clock_in_at)"
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                message TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'Open',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                customer_name TEXT NOT NULL,
                customer_email TEXT NOT NULL,
                items TEXT NOT NULL,
                total REAL NOT NULL CHECK (total >= 0),
                status TEXT NOT NULL DEFAULT 'Pending',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                payment_method TEXT NOT NULL DEFAULT 'cash'
                    CHECK (payment_method IN ('cash', 'card'))
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS customer_order_history_codes (
                email TEXT PRIMARY KEY,
                code_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                requested_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS pantry_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                quantity REAL NOT NULL CHECK (quantity >= 0),
                unit TEXT NOT NULL,
                reorder_level REAL NOT NULL CHECK (reorder_level >= 0),
                cost_per_unit REAL NOT NULL CHECK (cost_per_unit >= 0),
                supplier TEXT NOT NULL DEFAULT '',
                expiry_date TEXT,
                last_updated TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS stock_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity_change REAL NOT NULL,
                reason TEXT NOT NULL,
                supplier TEXT NOT NULL DEFAULT '',
                cost_per_unit REAL,
                movement_date TEXT NOT NULL,
                performed_by INTEGER NOT NULL,
                performed_by_name TEXT NOT NULL
            )
            """
        )
        order_columns = get_table_columns(connection, "orders")
        if "payment_method" not in order_columns:
            raise RuntimeError(
                "The orders database still uses the legacy bill-plan column. "
                "Stop the backend and run Backend/migrations/migrate_payment_method.py."
            )
        for table, column in [
            ("users", "is_active INTEGER NOT NULL DEFAULT 1"),
            ("menu_items", "image TEXT"),
            ("menu_items", "is_public INTEGER NOT NULL DEFAULT 0"),
            ("orders", "order_number TEXT"),
            ("orders", "tracking_token_hash TEXT"),
            ("orders", "waiter_id INTEGER"),
            ("orders", "order_type TEXT NOT NULL DEFAULT 'Dine in'"),
            ("orders", "table_number INTEGER"),
            ("orders", "payment_requested INTEGER NOT NULL DEFAULT 0"),
            ("orders", "rating INTEGER"),
            ("complaints", "order_id INTEGER"),
            ("complaints", "order_number TEXT"),
            ("complaints", "category TEXT"),
            ("complaints", "severity TEXT"),
            ("complaints", "description TEXT"),
            ("complaints", "waiter_id INTEGER"),
            ("complaints", "manager_id INTEGER"),
            ("complaints", "reference TEXT"),
            ("complaints", "identity_mode TEXT NOT NULL DEFAULT 'anonymous'"),
            ("complaints", "manager_response TEXT"),
            ("complaints", "responded_at TEXT"),
            ("complaints", "resolved_at TEXT"),
            ("complaints", "email_status TEXT NOT NULL DEFAULT 'pending'"),
            ("complaints", "sent_at TEXT"),
            ("complaints", "sent_by INTEGER"),
            ("complaints", "email_error TEXT"),
            ("complaints", "email_started_at TEXT"),
        ]:
            column_name = column.split(maxsplit=1)[0]
            existing_columns = get_table_columns(connection, table)
            if column_name not in existing_columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        connection.execute("UPDATE orders SET status = 'Received' WHERE status = 'Pending'")
        connection.execute(
            "UPDATE orders SET status = 'Preparing' WHERE status = 'In progress'"
        )
        connection.execute(
        """
        INSERT OR IGNORE INTO staff_roster
            (user_id, firstname, lastname, email, role, hourly_rate, created_at)
        SELECT id, firstname, lastname, email, role,
               CASE role WHEN 'manager' THEN ? ELSE ? END, created_at
        FROM users
        WHERE is_active = 1
            """,
            (STAFF_HOURLY_RATES["manager"], STAFF_HOURLY_RATES["waiter"]),
        )

        if seed_menu:
            menu_items = load_menu_items_from_csv()
            connection.executemany(
                """
                INSERT INTO menu_items
                    (name, category, description, price, icon, is_public)
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(name) DO UPDATE SET
                    category = excluded.category,
                    description = excluded.description,
                    price = excluded.price,
                    icon = excluded.icon,
                    is_public = 0
                """,
                menu_items,
            )
            connection.executemany(
                """
                INSERT INTO menu_items
                    (name, category, description, price, icon, image, is_public)
                VALUES (?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(name) DO UPDATE SET
                    category = excluded.category,
                    description = excluded.description,
                    price = excluded.price,
                    icon = excluded.icon,
                    image = excluded.image,
                    is_public = 1
                """,
                load_signature_menu_from_csv(),
            )
            connection.execute(
                "UPDATE menu_items SET image = CASE LOWER(category) "
                "WHEN 'roll' THEN 'https://images.unsplash.com/photo-1579871494447-9811cf80d66c?auto=format&fit=crop&w=900&q=85' "
                "WHEN 'nigiri' THEN 'https://images.unsplash.com/photo-1611143669185-af224c5e3252?auto=format&fit=crop&w=900&q=85' "
                "WHEN 'drink' THEN 'https://images.unsplash.com/photo-1544145945-f90425340c7e?auto=format&fit=crop&w=900&q=85' "
                "ELSE 'https://images.unsplash.com/photo-1569718212165-3a8278d5f624?auto=format&fit=crop&w=900&q=85' END "
                "WHERE image IS NULL OR image = ''"
            )


def complaint_to_dict(row):
    complaint = dict(row)
    complaint.setdefault("email_status", "pending")
    complaint.setdefault("sent_at", None)
    complaint.setdefault("sent_by", None)
    complaint.setdefault("email_error", None)
    return complaint


def pantry_item_to_dict(row):
    item = dict(row)
    item["status"] = pantry_status(item)
    return item


def pantry_status(item):
    today = datetime.now(timezone.utc).date()
    expiry_date = date_value = item["expiry_date"]
    if expiry_date:
        date_value = datetime.strptime(expiry_date, "%Y-%m-%d").date()
        if date_value < today:
            return "Expired"
    if item["quantity"] <= 0:
        return "Out of Stock"
    if item["quantity"] <= item["reorder_level"]:
        return "Low Stock"
    if date_value and date_value <= today + timedelta(days=7):
        return "Expiring Soon"
    return "In Stock"


def pantry_number(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number greater than or equal to 0")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a number greater than or equal to 0") from None
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{label} must be a number greater than or equal to 0")
    return number


def user_to_dict(row):
    return {
        "id": row["id"],
        "firstname": row["firstname"],
        "lastname": row["lastname"],
        "email": row["email"],
        "role": row["role"],
    }


def order_to_dict(row):
    order = dict(row)
    order["items"] = json.loads(order["items"])
    return order


def public_order(row):
    order = order_to_dict(row)
    return {
        "id": order["id"],
        "order_number": order.get("order_number") or f"BDU-{order['id']:05d}",
        "items": order["items"],
        "total": order["total"],
        "status": order["status"],
        "created_at": order["created_at"],
        "updated_at": order["updated_at"],
        "order_type": order["order_type"],
        "payment_method": order["payment_method"],
        "table_number": order["table_number"],
        "payment_requested": bool(order.get("payment_requested", 0)),
        "rating": order["rating"],
        "waiter_id": order.get("waiter_id"),
        "waiter_name": order.get("waiter_name"),
        "customer_name": order.get("customer_name"),
        "customer_email": order.get("customer_email"),
    }


def tracking_token_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def order_tracking_is_valid(order, token):
    stored_hash = order["tracking_token_hash"]
    return bool(
        stored_hash
        and token
        and hmac.compare_digest(stored_hash, tracking_token_hash(token))
    )


def customer_order_history_email():
    authorization = request.headers.get("Authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    try:
        claims = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=["HS256"],
            issuer="bundu-customer-order-history",
        )
    except jwt.exceptions.PyJWTError:
        return False
    email = claims.get("sub")
    if not isinstance(email, str) or not EMAIL_PATTERN.fullmatch(email):
        return False
    return email.lower()


def customer_order_history_code_hash(email, code):
    value = f"{email}:{code}".encode("utf-8")
    return hmac.new(JWT_SECRET.encode("utf-8"), value, hashlib.sha256).hexdigest()


def parse_iso_datetime(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def regular_hours_in_daily_window(start, end, day):
    window_start = datetime.combine(
        day, time(7), tzinfo=SOUTH_AFRICA_TIMEZONE
    ).astimezone(timezone.utc)
    window_end = datetime.combine(
        day, time(22), tzinfo=SOUTH_AFRICA_TIMEZONE
    ).astimezone(timezone.utc)
    overlap_start = max(start, window_start)
    overlap_end = min(end, window_end)
    return max(0.0, (overlap_end - overlap_start).total_seconds() / 3600)


def available_tables_for_period(connection, starts_at, ends_at):
    occupied = {
        str(row["table_number"])
        for row in connection.execute(
            """
            SELECT table_number FROM orders
            WHERE table_number IS NOT NULL
              AND status NOT IN ('Paid', 'Cancelled')
            """
        ).fetchall()
    }
    reserved = {
        str(row["table_number"])
        for row in connection.execute(
            """
            SELECT table_number FROM order_table_reservations
            WHERE status != 'Cancelled'
              AND starts_at < ? AND ends_at > ?
            """,
            (ends_at.isoformat(), starts_at.isoformat()),
        ).fetchall()
    }
    legacy_start = starts_at.astimezone(SOUTH_AFRICA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    legacy_end = ends_at.astimezone(SOUTH_AFRICA_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S")
    reserved.update(
        str(row["table_number"])
        for row in connection.execute(
            """
            SELECT table_number FROM reservations
            WHERE LOWER(status) NOT IN ('cancelled', 'completed', 'no-show', 'no show')
              AND datetime(reservation_at) < datetime(?)
              AND datetime(reservation_at, '+' || duration_minutes || ' minutes') > datetime(?)
            """,
            (legacy_end, legacy_start),
        ).fetchall()
    )
    return [table for table in sorted(TABLE_NUMBERS) if table not in occupied | reserved]


def calculate_monthly_attendance(records, month):
    month_start = datetime.strptime(f"{month}-01", "%Y-%m-%d").replace(
        tzinfo=SOUTH_AFRICA_TIMEZONE
    )
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    month_end = next_month.astimezone(timezone.utc)
    month_start_utc = month_start.astimezone(timezone.utc)
    now_utc = datetime.now(timezone.utc)
    weekly_hours = {}
    attendance_slices = []
    intervals_by_staff = {}
    for record in records:
        start = parse_iso_datetime(record["clock_in_at"])
        end = parse_iso_datetime(record["clock_out_at"]) if record["clock_out_at"] else now_utc
        if not start or not end or end <= start:
            continue
        intervals_by_staff.setdefault(record["user_id"], []).append(
            (start, end, record)
        )

    merged_records = []
    for intervals in intervals_by_staff.values():
        intervals.sort(key=lambda interval: interval[0])
        merged_start, merged_end, base_record = intervals[0]
        for start, end, record in intervals[1:]:
            if start <= merged_end:
                merged_end = max(merged_end, end)
                continue
            merged_records.append(
                {**base_record, "clock_in_at": merged_start.isoformat(), "clock_out_at": merged_end.isoformat()}
            )
            merged_start, merged_end, base_record = start, end, record
        merged_records.append(
            {**base_record, "clock_in_at": merged_start.isoformat(), "clock_out_at": merged_end.isoformat()}
        )
    ordered_records = sorted(
        merged_records, key=lambda record: record["clock_in_at"]
    )
    for record in ordered_records:
        start = parse_iso_datetime(record["clock_in_at"])
        end = parse_iso_datetime(record["clock_out_at"])
        if not start or not end or end <= start:
            continue
        cursor = start
        while cursor < end:
            local_cursor = cursor.astimezone(SOUTH_AFRICA_TIMEZONE)
            local_week_start = local_cursor.date() - timedelta(days=local_cursor.weekday())
            next_week_local = datetime.combine(
                local_week_start + timedelta(days=7),
                datetime.min.time(),
                tzinfo=SOUTH_AFRICA_TIMEZONE,
            )
            next_day_local = datetime.combine(
                local_cursor.date() + timedelta(days=1),
                datetime.min.time(),
                tzinfo=SOUTH_AFRICA_TIMEZONE,
            )
            segment_end = min(
                end,
                next_week_local.astimezone(timezone.utc),
                next_day_local.astimezone(timezone.utc),
            )
            hours = (segment_end - cursor).total_seconds() / 3600
            week_key = (record["user_id"], local_week_start.isoformat())
            used_hours = weekly_hours.get(week_key, 0.0)
            scheduled_hours = regular_hours_in_daily_window(
                cursor, segment_end, local_cursor.date()
            )
            regular_hours = min(
                scheduled_hours, max(0.0, WEEKLY_OVERTIME_THRESHOLD_HOURS - used_hours)
            )
            overtime_hours = max(0.0, hours - regular_hours)
            weekly_hours[week_key] = used_hours + hours
            day = local_cursor.date().isoformat()
            attendance_slices.append(
                {
                    "user_id": record["user_id"],
                    "name": f'{record["firstname"]} {record["lastname"]}',
                    "role": record["role"],
                    "hourly_rate": (
                        record["hourly_rate"]
                        if "hourly_rate" in record.keys()
                        else STAFF_HOURLY_RATES.get(record["role"], 0.0)
                    ),
                    "day": day,
                    "regular_hours": regular_hours,
                    "overtime_hours": overtime_hours,
                    "starts_at": cursor,
                    "ends_at": segment_end,
                }
            )
            cursor = segment_end

    daily_totals = {}
    staff_totals = {}
    for item in attendance_slices:
        if item["starts_at"] >= month_end or item["ends_at"] <= month_start_utc:
            continue
        staff_key = item["user_id"]
        rate = float(item["hourly_rate"])
        daily_key = (staff_key, item["day"])
        daily = daily_totals.setdefault(
            daily_key,
            {
                "user_id": staff_key,
                "name": item["name"],
                "role": item["role"],
                "hourly_rate": rate,
                "date": item["day"],
                "regular_hours": 0.0,
                "overtime_hours": 0.0,
                "pay": 0.0,
            },
        )
        staff = staff_totals.setdefault(
            staff_key,
            {
                "user_id": staff_key,
                "name": item["name"],
                "role": item["role"],
                "hourly_rate": rate,
                "regular_hours": 0.0,
                "overtime_hours": 0.0,
                "pay": 0.0,
            },
        )
        daily["regular_hours"] += item["regular_hours"]
        daily["overtime_hours"] += item["overtime_hours"]
        daily["pay"] += (
            item["regular_hours"] * rate
            + item["overtime_hours"] * rate * OVERTIME_MULTIPLIER
        )
        staff["regular_hours"] += item["regular_hours"]
        staff["overtime_hours"] += item["overtime_hours"]
        staff["pay"] += (
            item["regular_hours"] * rate
            + item["overtime_hours"] * rate * OVERTIME_MULTIPLIER
        )

    round_fields = ("regular_hours", "overtime_hours", "pay")
    daily = sorted(daily_totals.values(), key=lambda item: (item["date"], item["name"]))
    staff = sorted(staff_totals.values(), key=lambda item: item["name"])
    for item in daily + staff:
        for field in round_fields:
            item[field] = round(item[field], 2)
    return {"month": month, "daily": daily, "staff": staff}


def create_token(user, session_id):
    return jwt.encode(
        {
            "sub": str(user["id"]),
            "email": user["email"],
            "role": user["role"],
            "sid": session_id,
            "exp": datetime.now(timezone.utc).timestamp() + 86400,
            "iss": "bundu-api",
        },
        JWT_SECRET,
        algorithm="HS256",
    )


def require_auth(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            app.logger.warning(
                "Authentication rejected %s %s: bearer token missing",
                request.method,
                request.path,
            )
            return jsonify({
                "error": "A valid bearer token is required",
                "code": "authentication_required",
            }), 401

        try:
            claims = jwt.decode(
                token,
                JWT_SECRET,
                algorithms=["HS256"],
                issuer="bundu-api",
            )
        except jwt.exceptions.PyJWTError:
            app.logger.warning(
                "Authentication rejected %s %s: invalid or expired token",
                request.method,
                request.path,
            )
            return jsonify({
                "error": "Invalid or expired token",
                "code": "invalid_or_expired_token",
            }), 401

        with get_connection() as connection:
            user = connection.execute(
                """
                SELECT id, firstname, lastname, email, role
                FROM users WHERE id = ? AND is_active = 1
                """,
                (claims.get("sub"),),
            ).fetchone()
        if not user:
            app.logger.warning(
                "Authentication rejected %s %s: account unavailable",
                request.method,
                request.path,
            )
            return jsonify({
                "error": "Account is no longer available",
                "code": "account_unavailable",
            }), 401
        g.current_user = dict(user)
        g.auth_session_id = claims.get("sid")
        return view(*args, **kwargs)

    return wrapped


def require_role(*roles):
    def decorator(view):
        @wraps(view)
        @require_auth
        def wrapped(*args, **kwargs):
            if g.current_user.get("role") not in roles:
                app.logger.warning(
                    "Authorization rejected %s %s: role %s is not in %s",
                    request.method,
                    request.path,
                    g.current_user.get("role"),
                    roles,
                )
                return jsonify({
                    "error": "You do not have permission to access this resource",
                    "code": "forbidden",
                }), 403
            return view(*args, **kwargs)

        return wrapped

    return decorator


@app.post("/api/auth/register")
@limiter.limit("5 per hour")
def register_user():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid registration payload is required"}), 400
    firstname = str(payload.get("firstname", "")).strip()
    lastname = str(payload.get("lastname", "")).strip()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    role = str(payload.get("role", "waiter")).strip().lower()

    if (
        not 1 <= len(firstname) <= 80
        or not 1 <= len(lastname) <= 80
        or len(email) > 254
        or not EMAIL_PATTERN.fullmatch(email)
        or not 12 <= len(password) <= 128
        or role not in VALID_ROLES
    ):
        return jsonify({"error": "Enter valid names and email, a password between 12 and 128 characters, and a valid role"}), 400
    if role == "manager":
        expected_key = MANAGER_REGISTRATION_KEY
        supplied_key = str(payload.get("manager_registration_key", ""))
    else:
        expected_key = WAITER_REGISTRATION_KEY
        supplied_key = str(payload.get("registration_key", ""))
    if not IS_DEVELOPMENT and not hmac.compare_digest(supplied_key, expected_key):
        return jsonify({"error": "A valid staff registration key is required"}), 403

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                INSERT INTO users (firstname, lastname, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (firstname, lastname, email, generate_password_hash(password, method="scrypt"), role, timestamp),
            )
            connection.execute(
                """
                INSERT INTO staff_roster
                    (user_id, firstname, lastname, email, role, hourly_rate, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cursor.lastrowid,
                    firstname,
                    lastname,
                    email,
                    role,
                    STAFF_HOURLY_RATES[role],
                    timestamp,
                ),
            )
    except DATABASE_INTEGRITY_ERRORS:
        return jsonify({"error": "An account with that email already exists"}), 409

    return jsonify({"message": "Account created successfully"}), 201


@app.post("/api/auth/login")
@limiter.limit("5 per minute")
def login_user():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Incorrect email or password"}), 401
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email) or not 1 <= len(password) <= 128:
        return jsonify({"error": "Incorrect email or password"}), 401

    with get_connection() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE email = ? COLLATE NOCASE AND is_active = 1",
            (email,),
        ).fetchone()

    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify({"error": "Incorrect email or password"}), 401

    session_id = secrets.token_urlsafe(32)
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE staff_password_history
            SET status = 'used', used_at = ?
            WHERE user_id = ? AND status = 'current'
            """,
            (datetime.now(timezone.utc).isoformat(), user["id"]),
        )
        connection.execute(
            """
            INSERT INTO attendance_records (user_id, session_id, clock_in_at)
            VALUES (?, ?, ?)
            """,
            (user["id"], session_id, datetime.now(timezone.utc).isoformat()),
        )
    return jsonify({
        "token": create_token(user, session_id),
        "user": user_to_dict(user),
    })


@app.post("/api/auth/logout")
@require_auth
def logout_user():
    if g.auth_session_id:
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE attendance_records
                SET clock_out_at = ?
                WHERE user_id = ? AND session_id = ? AND clock_out_at IS NULL
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    g.current_user["id"],
                    g.auth_session_id,
                ),
            )
    return jsonify({"message": "Signed out successfully"})


@app.get("/api/auth/me")
@require_auth
def get_current_user():
    return jsonify(user_to_dict(g.current_user))


@app.get("/api/menu-items")
def list_menu_items():
    with get_connection() as connection:
        menu_items = connection.execute(
            "SELECT * FROM menu_items WHERE is_public = 1 ORDER BY id"
        ).fetchall()

    return jsonify([dict(menu_item) for menu_item in menu_items])


@app.get("/api/menu-items/signature")
def list_signature_menu_items():
    return list_menu_items()


@app.post("/api/menu-items")
@require_role("manager")
def create_menu_item():
    payload = request.form if request.mimetype == "multipart/form-data" else (request.get_json(silent=True) or {})
    name = str(payload.get("name", "")).strip()
    category = str(payload.get("category", "Side")).strip() or "Side"
    description = str(payload.get("description", "Freshly prepared DineDesk favourite.")).strip()
    image = str(payload.get("image", "")).strip()
    uploaded_image = request.files.get("image") if request.mimetype == "multipart/form-data" else None
    try:
        price = float(payload.get("price"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid price is required"}), 400
    if (
        not 1 <= len(name) <= 120
        or not math.isfinite(price)
        or price < 0
        or len(category) > 40
        or len(description) > 500
    ):
        return jsonify({"error": "Name and a valid price are required"}), 400
    if uploaded_image and uploaded_image.filename:
        safe_filename = secure_filename(uploaded_image.filename)
        extension = Path(safe_filename).suffix.lower()
        if extension not in ALLOWED_IMAGE_EXTENSIONS or not uploaded_image.mimetype.startswith("image/"):
            return jsonify({"error": "Upload a JPG, PNG, WebP or GIF image"}), 400
        contents = uploaded_image.read(MAX_IMAGE_SIZE + 1)
        if len(contents) > MAX_IMAGE_SIZE:
            return jsonify({"error": "Images must be 5 MB or smaller"}), 413
        UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex}{extension}"
        image_path = UPLOAD_FOLDER / filename
        image_path.write_bytes(contents)
        image = f"/uploads/{filename}"
    elif image and not image.startswith(("https://", "/uploads/")):
        return jsonify({"error": "Menu images must use HTTPS or an uploaded image"}), 400
    try:
        with get_connection() as connection:
            cursor = connection.execute(
                "INSERT INTO menu_items (name, category, description, price, icon, image, is_public) VALUES (?, ?, ?, ?, ?, ?, 1)",
                (name, category, description, price, "🍽", image),
            )
            item = connection.execute("SELECT * FROM menu_items WHERE id = ?", (cursor.lastrowid,)).fetchone()
    except DATABASE_INTEGRITY_ERRORS:
        return jsonify({"error": "A menu item with that name already exists"}), 409
    return jsonify(dict(item)), 201


@app.get("/uploads/<path:filename>")
def uploaded_menu_image(filename):
    return send_from_directory(UPLOAD_FOLDER, filename)


@app.delete("/api/menu-items/<int:item_id>")
@require_role("manager")
def delete_menu_item(item_id):
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM menu_items WHERE id = ?", (item_id,))
    if cursor.rowcount == 0:
        return jsonify({"error": "Menu item not found"}), 404
    return jsonify({"message": "Menu item deleted"})


@app.get("/api/promotions")
@require_role("manager")
def list_promotions():
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT promotions.*, menu_items.name AS menu_name FROM promotions "
            "JOIN menu_items ON menu_items.id = promotions.menu_item_id "
            "WHERE menu_items.is_public = 1 ORDER BY promotions.created_at DESC"
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/api/promotions/active")
def list_active_promotions():
    current_time = datetime.now().astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    with get_connection() as connection:
        rows = connection.execute(
            """
            SELECT promotions.*, menu_items.name AS menu_name, menu_items.price AS original_price
            FROM promotions
            JOIN menu_items ON menu_items.id = promotions.menu_item_id
            WHERE menu_items.is_public = 1
              AND promotions.start_at <= ? AND promotions.end_at >= ?
            ORDER BY promotions.discount_percent DESC, promotions.created_at DESC
            """,
            (current_time, current_time),
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.post("/api/promotions")
@require_role("manager")
def create_promotion():
    payload = request.get_json(silent=True) or {}
    try:
        menu_item_id = int(payload.get("menu_item_id"))
        discount_percent = int(payload.get("discount_percent"))
    except (TypeError, ValueError):
        return jsonify({"error": "A menu item and valid discount are required"}), 400
    start_at = str(payload.get("start_at", "")).strip()
    end_at = str(payload.get("end_at", "")).strip()
    if not 1 <= discount_percent <= 100 or not start_at or not end_at:
        return jsonify({"error": "Discount and a valid promotion period are required"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        item = connection.execute(
            "SELECT id FROM menu_items WHERE id = ? AND is_public = 1",
            (menu_item_id,),
        ).fetchone()
        if not item:
            return jsonify({"error": "Menu item not found"}), 404
        cursor = connection.execute(
            "INSERT INTO promotions (menu_item_id, discount_percent, start_at, end_at, created_at) VALUES (?, ?, ?, ?, ?)",
            (menu_item_id, discount_percent, start_at, end_at, timestamp),
        )
        promotion = connection.execute(
            "SELECT promotions.*, menu_items.name AS menu_name FROM promotions JOIN menu_items ON menu_items.id = promotions.menu_item_id WHERE promotions.id = ?",
            (cursor.lastrowid,),
        ).fetchone()
    return jsonify(dict(promotion)), 201


@app.get("/api/shifts")
@require_role("manager")
def list_shifts():
    with get_connection() as connection:
        shifts = connection.execute("SELECT * FROM shifts ORDER BY created_at DESC").fetchall()
    return jsonify([dict(shift) for shift in shifts])


@app.get("/api/attendance")
@require_role("manager")
def list_attendance():
    month = request.args.get("month")
    if not month:
        month = datetime.now(SOUTH_AFRICA_TIMEZONE).strftime("%Y-%m")
    try:
        datetime.strptime(month, "%Y-%m")
    except ValueError:
        return jsonify({"error": "Month must use YYYY-MM format"}), 400
    with get_connection() as connection:
        on_duty = connection.execute(
            """
            SELECT users.id AS user_id, users.firstname, users.lastname, users.role,
                   attendance_records.id AS attendance_id,
                   attendance_records.clock_in_at
            FROM attendance_records
            JOIN users ON users.id = attendance_records.user_id
            WHERE attendance_records.clock_out_at IS NULL
            ORDER BY clock_in_at
            """
        ).fetchall()
        history = connection.execute(
            """
            SELECT attendance_records.id, users.firstname, users.lastname, users.role,
                   attendance_records.clock_in_at, attendance_records.clock_out_at,
                   attendance_records.user_id
            FROM attendance_records
            JOIN users ON users.id = attendance_records.user_id
            ORDER BY attendance_records.clock_in_at DESC
            LIMIT 100
            """
        ).fetchall()
        payroll_records = connection.execute(
            """
            SELECT attendance_records.user_id, users.firstname, users.lastname, users.role,
                   attendance_records.clock_in_at, attendance_records.clock_out_at,
                   COALESCE(staff_roster.hourly_rate, CASE users.role
                       WHEN 'manager' THEN ? ELSE ? END) AS hourly_rate
            FROM attendance_records
            JOIN users ON users.id = attendance_records.user_id
            LEFT JOIN staff_roster ON staff_roster.user_id = users.id
            ORDER BY attendance_records.clock_in_at
            """,
            (STAFF_HOURLY_RATES["manager"], STAFF_HOURLY_RATES["waiter"]),
        ).fetchall()
        staff_members = connection.execute(
            """
            SELECT id, user_id, firstname, lastname, role, hourly_rate
            FROM staff_roster
            ORDER BY firstname COLLATE NOCASE, lastname COLLATE NOCASE
            """
        ).fetchall()
    now_utc = datetime.now(timezone.utc)
    today = now_utc.astimezone(SOUTH_AFRICA_TIMEZONE).date()
    today_start = datetime.combine(
        today, datetime.min.time(), tzinfo=SOUTH_AFRICA_TIMEZONE
    ).astimezone(timezone.utc)
    on_duty_items = []
    for row in on_duty:
        item = dict(row)
        clock_in = parse_iso_datetime(item["clock_in_at"])
        if clock_in:
            work_start = max(clock_in, today_start)
            elapsed_hours = max(0.0, (now_utc - work_start).total_seconds() / 3600)
            regular_hours = regular_hours_in_daily_window(work_start, now_utc, today)
            item["today_regular_hours"] = round(regular_hours, 2)
            item["today_overtime_hours"] = round(
                max(0.0, elapsed_hours - regular_hours), 2
            )
        else:
            item["today_regular_hours"] = 0.0
            item["today_overtime_hours"] = 0.0
        on_duty_items.append(item)
    payroll = calculate_monthly_attendance(payroll_records, month)
    reported_staff_ids = {member["user_id"] for member in payroll["staff"]}
    for member in staff_members:
        if member["user_id"] in reported_staff_ids:
            continue
        payroll["staff"].append({
            "staff_roster_id": member["id"],
            "user_id": member["user_id"],
            "name": f'{member["firstname"]} {member["lastname"]}',
            "role": member["role"],
            "hourly_rate": member["hourly_rate"],
            "regular_hours": 0.0,
            "overtime_hours": 0.0,
            "pay": 0.0,
        })
    payroll["staff"].sort(key=lambda item: item["name"].casefold())
    return jsonify({
        "on_duty": on_duty_items,
        "history": [dict(row) for row in history],
        "payroll": payroll,
        "hourly_rates": STAFF_HOURLY_RATES,
        "overtime_threshold_hours": WEEKLY_OVERTIME_THRESHOLD_HOURS,
        "overtime_multiplier": OVERTIME_MULTIPLIER,
    })


@app.post("/api/shifts")
@require_role("manager")
def create_shift():
    payload = request.get_json(silent=True) or {}
    staff_name = str(payload.get("staff_name", "")).strip()
    role = str(payload.get("role", "Waiter")).strip()
    shift_label = str(payload.get("shift_label", "")).strip()
    status = str(payload.get("status", "Scheduled")).strip()
    if not staff_name or not shift_label:
        return jsonify({"error": "Staff member and shift are required"}), 400
    with get_connection() as connection:
        cursor = connection.execute(
            "INSERT INTO shifts (staff_name, role, shift_label, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (staff_name, role, shift_label, status, datetime.now(timezone.utc).isoformat()),
        )
        shift = connection.execute("SELECT * FROM shifts WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return jsonify(dict(shift)), 201


@app.get("/api/staff")
@require_role("manager")
def list_staff():
    with get_connection() as connection:
        staff = connection.execute(
            """
            SELECT staff_roster.*, users.role AS login_role,
                   COALESCE(users.is_active, 0) AS login_active
            FROM staff_roster
            LEFT JOIN users ON users.id = staff_roster.user_id
            ORDER BY staff_roster.firstname COLLATE NOCASE, staff_roster.lastname COLLATE NOCASE
            """
        ).fetchall()
    return jsonify([dict(member) for member in staff])


@app.post("/api/staff")
@require_role("manager")
def create_staff_member():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid staff member is required"}), 400
    firstname = str(payload.get("firstname", "")).strip()
    lastname = str(payload.get("lastname", "")).strip()
    role = str(payload.get("role", "")).strip().lower()
    email = str(payload.get("email", "")).strip().lower()
    password = str(payload.get("password", ""))
    try:
        hourly_rate = float(payload.get("hourly_rate"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid hourly pay rate"}), 400
    if (
        not 1 <= len(firstname) <= 80
        or not 1 <= len(lastname) <= 80
        or role not in STAFF_ROSTER_ROLES
        or not math.isfinite(hourly_rate)
        or hourly_rate < 0
    ):
        return jsonify({"error": "Enter a valid name, role, and non-negative hourly pay rate"}), 400
    if email and (len(email) > 254 or not EMAIL_PATTERN.fullmatch(email)):
        return jsonify({"error": "Enter a valid email address"}), 400
    if role in {"waiter", "manager"} and (
        not email or (password and not 12 <= len(password) <= 128)
    ):
        return jsonify({
            "error": "Waiters and managers need a valid email address; an optional password must be 12 to 128 characters"
        }), 400
    if role == "chef" and password:
        return jsonify({"error": "Chefs are roster-only and do not have a sign-in account"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user_id = None
            if role in {"waiter", "manager"}:
                stored_password = password or secrets.token_urlsafe(32)
                existing_user = connection.execute(
                    "SELECT id, is_active FROM users WHERE email = ? COLLATE NOCASE",
                    (email,),
                ).fetchone()
                if existing_user:
                    if existing_user["is_active"]:
                        return jsonify({"error": "An active account already uses that email"}), 409
                    user_id = existing_user["id"]
                    connection.execute(
                        """
                        UPDATE users
                        SET firstname = ?, lastname = ?, password_hash = ?, role = ?, is_active = 1
                        WHERE id = ?
                        """,
                        (
                            firstname, lastname,
                            generate_password_hash(stored_password, method="scrypt"),
                            role, user_id,
                        ),
                    )
                    connection.execute(
                        """
                        UPDATE staff_password_history SET status = 'revoked'
                        WHERE user_id = ? AND status = 'current'
                        """,
                        (user_id,),
                    )
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO users
                            (firstname, lastname, email, password_hash, role, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            firstname,
                            lastname,
                            email,
                            generate_password_hash(stored_password, method="scrypt"),
                            role,
                            timestamp,
                        ),
                    )
                    user_id = cursor.lastrowid
            cursor = connection.execute(
                """
                INSERT INTO staff_roster
                    (user_id, firstname, lastname, email, role, hourly_rate, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (user_id, firstname, lastname, email or None, role, hourly_rate, timestamp),
            )
            member = connection.execute(
                "SELECT * FROM staff_roster WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
    except DATABASE_INTEGRITY_ERRORS:
        return jsonify({"error": "A staff account or roster member already uses that email"}), 409
    return jsonify(dict(member)), 201


@app.put("/api/staff/<int:staff_id>")
@require_role("manager")
def update_staff_member(staff_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid staff member is required"}), 400
    firstname = str(payload.get("firstname", "")).strip()
    lastname = str(payload.get("lastname", "")).strip()
    role = str(payload.get("role", "")).strip().lower()
    email = str(payload.get("email", "")).strip().lower()
    try:
        hourly_rate = float(payload.get("hourly_rate"))
    except (TypeError, ValueError):
        return jsonify({"error": "Enter a valid hourly pay rate"}), 400
    if (
        not 1 <= len(firstname) <= 80
        or not 1 <= len(lastname) <= 80
        or role not in STAFF_ROSTER_ROLES
        or not math.isfinite(hourly_rate)
        or hourly_rate < 0
    ):
        return jsonify({"error": "Enter a valid name, role, and non-negative hourly pay rate"}), 400
    if role in {"waiter", "manager"} and (
        len(email) > 254 or not EMAIL_PATTERN.fullmatch(email)
    ):
        return jsonify({"error": "Waiters and managers need a valid email address"}), 400
    if email and (len(email) > 254 or not EMAIL_PATTERN.fullmatch(email)):
        return jsonify({"error": "Enter a valid email address"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            member = connection.execute(
                "SELECT * FROM staff_roster WHERE id = ?", (staff_id,)
            ).fetchone()
            if not member:
                return jsonify({"error": "Staff member not found"}), 404

            user_id = member["user_id"]
            if (
                user_id == g.current_user["id"]
                and g.current_user["role"] == "manager"
                and role != "manager"
            ):
                return jsonify({"error": "You cannot remove your own manager access"}), 400
            if role in {"waiter", "manager"}:
                existing_email = connection.execute(
                    "SELECT id, is_active FROM users WHERE email = ? COLLATE NOCASE",
                    (email,),
                ).fetchone()
                if existing_email and existing_email["id"] != user_id and existing_email["is_active"]:
                    return jsonify({"error": "An active account already uses that email"}), 409
                if user_id:
                    current_user = connection.execute(
                        "SELECT email, is_active FROM users WHERE id = ?", (user_id,)
                    ).fetchone()
                    if not current_user:
                        user_id = None
                if user_id:
                    reset_password = (
                        current_user["email"].casefold() != email.casefold()
                        or not current_user["is_active"]
                    )
                    if reset_password:
                        connection.execute(
                            """
                            UPDATE staff_password_history SET status = 'revoked'
                            WHERE user_id = ? AND status = 'current'
                            """,
                            (user_id,),
                        )
                    connection.execute(
                        """
                        UPDATE users
                        SET firstname = ?, lastname = ?, email = ?, role = ?, is_active = 1,
                            password_hash = CASE WHEN ? THEN ? ELSE password_hash END
                        WHERE id = ?
                        """,
                        (
                            firstname, lastname, email, role, int(reset_password),
                            generate_password_hash(secrets.token_urlsafe(32), method="scrypt")
                            if reset_password else "",
                            user_id,
                        ),
                    )
                elif existing_email:
                    user_id = existing_email["id"]
                    connection.execute(
                        """
                        UPDATE staff_password_history SET status = 'revoked'
                        WHERE user_id = ? AND status = 'current'
                        """,
                        (user_id,),
                    )
                    connection.execute(
                        """
                        UPDATE users
                        SET firstname = ?, lastname = ?, role = ?, is_active = 1,
                            password_hash = ?
                        WHERE id = ?
                        """,
                        (
                            firstname, lastname, role,
                            generate_password_hash(secrets.token_urlsafe(32), method="scrypt"),
                            user_id,
                        ),
                    )
                else:
                    cursor = connection.execute(
                        """
                        INSERT INTO users
                            (firstname, lastname, email, password_hash, role, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            firstname, lastname, email,
                            generate_password_hash(secrets.token_urlsafe(32), method="scrypt"),
                            role, timestamp,
                        ),
                    )
                    user_id = cursor.lastrowid
            elif user_id:
                connection.execute(
                    "UPDATE users SET firstname = ?, lastname = ?, is_active = 0 WHERE id = ?",
                    (firstname, lastname, user_id),
                )
                connection.execute(
                    """
                    UPDATE attendance_records SET clock_out_at = ?
                    WHERE user_id = ? AND clock_out_at IS NULL
                    """,
                    (timestamp, user_id),
                )
                connection.execute(
                    """
                    UPDATE staff_password_history SET status = 'revoked'
                    WHERE user_id = ? AND status = 'current'
                    """,
                    (user_id,),
                )

            connection.execute(
                """
                UPDATE staff_roster
                SET user_id = ?, firstname = ?, lastname = ?, email = ?, role = ?, hourly_rate = ?
                WHERE id = ?
                """,
                (user_id, firstname, lastname, email or None, role, hourly_rate, staff_id),
            )
            updated = connection.execute(
                """
                SELECT staff_roster.*, users.role AS login_role,
                       COALESCE(users.is_active, 0) AS login_active
                FROM staff_roster
                LEFT JOIN users ON users.id = staff_roster.user_id
                WHERE staff_roster.id = ?
                """,
                (staff_id,),
            ).fetchone()
    except DATABASE_INTEGRITY_ERRORS:
        return jsonify({"error": "That email address is already assigned to another staff account"}), 409
    return jsonify(dict(updated))


@app.delete("/api/staff/<int:staff_id>")
@require_role("manager")
def delete_staff_member(staff_id):
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        member = connection.execute(
            "SELECT id, user_id FROM staff_roster WHERE id = ?", (staff_id,)
        ).fetchone()
        if not member:
            return jsonify({"error": "Staff member not found"}), 404
        if member["user_id"] == g.current_user["id"]:
            return jsonify({"error": "You cannot delete your own staff account"}), 400
        if member["user_id"]:
            connection.execute(
                """
                UPDATE users SET is_active = 0
                WHERE id = ? AND role IN ('waiter', 'manager')
                """,
                (member["user_id"],),
            )
            connection.execute(
                """
                UPDATE attendance_records SET clock_out_at = ?
                WHERE user_id = ? AND clock_out_at IS NULL
                """,
                (timestamp, member["user_id"]),
            )
            connection.execute(
                """
                UPDATE staff_password_history SET status = 'revoked'
                WHERE user_id = ? AND status = 'current'
                """,
                (member["user_id"],),
            )
        connection.execute(
            "DELETE FROM staff_roster_assignments WHERE staff_id = ?", (staff_id,)
        )
        connection.execute("DELETE FROM staff_roster WHERE id = ?", (staff_id,))
    return jsonify({"message": "Staff member deleted"})


@app.post("/api/staff/<int:staff_id>/password")
@require_role("manager")
def generate_staff_password(staff_id):
    password = secrets.token_urlsafe(18)
    password_hash = generate_password_hash(password, method="scrypt")
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            member = connection.execute(
                "SELECT * FROM staff_roster WHERE id = ?", (staff_id,)
            ).fetchone()
            if not member:
                return jsonify({"error": "Staff member not found"}), 404
            if member["role"] not in {"waiter", "manager"} or not member["email"]:
                return jsonify({
                    "error": "Set this staff member to waiter or manager and add their email before generating a password"
                }), 400

            user_id = member["user_id"]
            existing_user = connection.execute(
                "SELECT id, is_active FROM users WHERE email = ? COLLATE NOCASE",
                (member["email"],),
            ).fetchone()
            if existing_user and existing_user["id"] != user_id and existing_user["is_active"]:
                return jsonify({"error": "An active account already uses that email"}), 409
            if user_id:
                connection.execute(
                    """
                    UPDATE users
                    SET firstname = ?, lastname = ?, email = ?, role = ?, password_hash = ?,
                        is_active = 1
                    WHERE id = ?
                    """,
                    (
                        member["firstname"], member["lastname"], member["email"],
                        member["role"], password_hash, user_id,
                    ),
                )
            elif existing_user:
                user_id = existing_user["id"]
                connection.execute(
                    """
                    UPDATE users
                    SET firstname = ?, lastname = ?, password_hash = ?, role = ?, is_active = 1
                    WHERE id = ?
                    """,
                    (
                        member["firstname"], member["lastname"], password_hash,
                        member["role"], user_id,
                    ),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO users
                        (firstname, lastname, email, password_hash, role, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        member["firstname"], member["lastname"], member["email"],
                        password_hash, member["role"], timestamp,
                    ),
                )
                user_id = cursor.lastrowid
            connection.execute(
                "UPDATE staff_roster SET user_id = ? WHERE id = ?",
                (user_id, staff_id),
            )
            connection.execute(
                """
                UPDATE staff_password_history SET status = 'replaced'
                WHERE user_id = ? AND status = 'current'
                """,
                (user_id,),
            )
            generated_by_name = (
                f'{g.current_user["firstname"]} {g.current_user["lastname"]}'.strip()
            )
            connection.execute(
                """
                INSERT INTO staff_password_history
                    (staff_id, user_id, staff_name, email, generated_by,
                     generated_by_name, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    staff_id, user_id,
                    f'{member["firstname"]} {member["lastname"]}'.strip(),
                    member["email"], g.current_user["id"], generated_by_name, timestamp,
                ),
            )
    except DATABASE_INTEGRITY_ERRORS:
        return jsonify({"error": "Could not assign a password to that staff email"}), 409
    return jsonify({"email": member["email"], "password": password})


@app.get("/api/staff/password-history")
@require_role("manager")
def list_staff_password_history():
    with get_connection() as connection:
        history = connection.execute(
            """
            SELECT staff_name, email, generated_by_name, created_at, used_at, status
            FROM staff_password_history
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()
    return jsonify([dict(record) for record in history])


@app.get("/api/roster")
@require_role("manager")
def list_staff_roster():
    week_start_value = request.args.get("week_start")
    if week_start_value:
        try:
            week_start = datetime.strptime(week_start_value, "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Week start must use YYYY-MM-DD format"}), 400
    else:
        today = datetime.now(SOUTH_AFRICA_TIMEZONE).date()
        week_start = today - timedelta(days=today.weekday())
    week_end = week_start + timedelta(days=7)
    today = datetime.now(SOUTH_AFRICA_TIMEZONE)
    with get_connection() as connection:
        assignments = connection.execute(
            """
            SELECT staff_roster_assignments.*, staff_roster.firstname, staff_roster.lastname,
                   staff_roster.email, staff_roster.role, staff_roster.hourly_rate,
                   staff_roster.user_id
            FROM staff_roster_assignments
            JOIN staff_roster ON staff_roster.id = staff_roster_assignments.staff_id
            WHERE work_date >= ? AND work_date < ?
            ORDER BY work_date, starts_at, staff_roster.firstname COLLATE NOCASE
            """,
            (week_start.isoformat(), week_end.isoformat()),
        ).fetchall()
        result = []
        for assignment in assignments:
            item = dict(assignment)
            clocked_in = None
            if item["user_id"] is not None:
                local_start = datetime.combine(
                    date.fromisoformat(item["work_date"]),
                    datetime.min.time(),
                    tzinfo=SOUTH_AFRICA_TIMEZONE,
                )
                local_end = local_start + timedelta(days=1)
                attendance = connection.execute(
                    """
                    SELECT clock_in_at FROM attendance_records
                    WHERE user_id = ? AND clock_in_at >= ? AND clock_in_at < ?
                    ORDER BY clock_in_at LIMIT 1
                    """,
                    (
                        item["user_id"],
                        local_start.astimezone(timezone.utc).isoformat(),
                        local_end.astimezone(timezone.utc).isoformat(),
                    ),
                ).fetchone()
                clocked_in = attendance["clock_in_at"] if attendance else None
            shift_end = datetime.combine(
                date.fromisoformat(item["work_date"]),
                datetime.strptime(item["ends_at"], "%H:%M").time(),
                tzinfo=SOUTH_AFRICA_TIMEZONE,
            )
            if clocked_in:
                item["attendance_state"] = "Present"
            elif today.date() > date.fromisoformat(item["work_date"]) or today >= shift_end:
                item["attendance_state"] = "Absent"
            else:
                item["attendance_state"] = "Scheduled"
            item["clock_in_at"] = clocked_in
            result.append(item)
    return jsonify({"week_start": week_start.isoformat(), "assignments": result})


@app.post("/api/roster")
@require_role("manager")
def create_staff_roster_assignment():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid roster shift is required"}), 400
    try:
        staff_id = int(payload.get("staff_id"))
        work_date = datetime.strptime(
            str(payload.get("work_date", "")), "%Y-%m-%d"
        ).date()
    except (TypeError, ValueError):
        return jsonify({"error": "Choose a staff member and valid work date"}), 400
    shift_label = str(payload.get("shift_label", "")).strip()
    starts_at = str(payload.get("starts_at", "")).strip()
    ends_at = str(payload.get("ends_at", "")).strip()
    try:
        start_time = datetime.strptime(starts_at, "%H:%M").time()
        end_time = datetime.strptime(ends_at, "%H:%M").time()
    except ValueError:
        return jsonify({"error": "Shift start and end must use HH:MM"}), 400
    if not 1 <= len(shift_label) <= 120 or end_time <= start_time:
        return jsonify({"error": "Enter a shift label and an end time after its start"}), 400
    created_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        staff = connection.execute(
            "SELECT id FROM staff_roster WHERE id = ?", (staff_id,)
        ).fetchone()
        if not staff:
            return jsonify({"error": "Staff member not found"}), 404
        try:
            cursor = connection.execute(
                """
                INSERT INTO staff_roster_assignments
                    (staff_id, work_date, shift_label, starts_at, ends_at, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    staff_id,
                    work_date.isoformat(),
                    shift_label,
                    start_time.strftime("%H:%M"),
                    end_time.strftime("%H:%M"),
                    g.current_user["id"],
                    created_at,
                ),
            )
        except DATABASE_INTEGRITY_ERRORS:
            return jsonify({"error": "That staff member already has this shift on that date"}), 409
        assignment = connection.execute(
            """
            SELECT staff_roster_assignments.*, staff_roster.firstname, staff_roster.lastname,
                   staff_roster.role
            FROM staff_roster_assignments
            JOIN staff_roster ON staff_roster.id = staff_roster_assignments.staff_id
            WHERE staff_roster_assignments.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    return jsonify(dict(assignment)), 201


@app.post("/api/customer/order-history/request-code")
@limiter.limit("5 per hour")
def request_customer_order_history_code():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid email address is required"}), 400
    email = payload.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email):
        return jsonify({"error": "Enter a valid email address"}), 400

    timestamp = datetime.now(timezone.utc)
    code = f"{secrets.randbelow(1_000_000):06d}"
    code_hash = customer_order_history_code_hash(email, code)
    expires_at = (timestamp + timedelta(minutes=10)).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT requested_at FROM customer_order_history_codes WHERE email = ?",
            (email,),
        ).fetchone()
        if existing:
            requested_at = datetime.fromisoformat(existing["requested_at"])
            if timestamp - requested_at < timedelta(seconds=60):
                return jsonify({
                    "message": "If this email can receive a code, one will arrive shortly."
                })
        connection.execute(
            """
            INSERT INTO customer_order_history_codes
                (email, code_hash, expires_at, requested_at, attempts)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(email) DO UPDATE SET
                code_hash = excluded.code_hash,
                expires_at = excluded.expires_at,
                requested_at = excluded.requested_at,
                attempts = 0
            """,
            (email, code_hash, expires_at, timestamp.isoformat()),
        )

    message = EmailMessage()
    message["Subject"] = "Your DineDesk order-history verification code"
    message["From"] = os.environ.get("SMTP_FROM", "").strip() or os.environ.get("SMTP_USER", "").strip()
    message["To"] = email
    message.set_content(
        f"Your DineDesk verification code is {code}.\n\n"
        "It expires in 10 minutes. If you did not request this code, you can ignore this email."
    )
    try:
        send_email_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as error:
        app.logger.error("Could not send customer order-history verification email: %s", error)
        with get_connection() as connection:
            connection.execute(
                "DELETE FROM customer_order_history_codes WHERE email = ? AND code_hash = ?",
                (email, code_hash),
            )
        return jsonify({"error": "Could not send the verification email. Please try again later."}), 503
    return jsonify({"message": "If this email can receive a code, one will arrive shortly."})


@app.post("/api/customer/order-history/verify-code")
@limiter.limit("10 per hour")
def verify_customer_order_history_code():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "Enter the email address and six-digit code"}), 400
    email = payload.get("email")
    email = email.strip().lower() if isinstance(email, str) else ""
    code = payload.get("code")
    if (
        len(email) > 254
        or not EMAIL_PATTERN.fullmatch(email)
        or not isinstance(code, str)
        or not re.fullmatch(r"\d{6}", code)
    ):
        return jsonify({"error": "Enter the email address and six-digit code"}), 400

    timestamp = datetime.now(timezone.utc)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        record = connection.execute(
            "SELECT * FROM customer_order_history_codes WHERE email = ?",
            (email,),
        ).fetchone()
        valid = False
        if record:
            expires_at = datetime.fromisoformat(record["expires_at"])
            supplied_hash = customer_order_history_code_hash(email, code)
            valid = (
                record["attempts"] < 5
                and expires_at > timestamp
                and hmac.compare_digest(record["code_hash"], supplied_hash)
            )
            if valid:
                connection.execute(
                    "DELETE FROM customer_order_history_codes WHERE email = ?",
                    (email,),
                )
            else:
                connection.execute(
                    """
                    UPDATE customer_order_history_codes
                    SET attempts = attempts + 1
                    WHERE email = ?
                    """,
                    (email,),
                )
                connection.execute(
                    "DELETE FROM customer_order_history_codes WHERE email = ? AND attempts >= 5",
                    (email,),
                )
        if not valid:
            return jsonify({"error": "The verification code is invalid or expired"}), 400

    token = jwt.encode(
        {
            "sub": email,
            "exp": (timestamp + timedelta(hours=12)).timestamp(),
            "iss": "bundu-customer-order-history",
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    return jsonify({"email": email, "token": token})


@app.get("/api/customer/orders")
@limiter.limit("60 per hour")
def list_customer_orders():
    email = customer_order_history_email()
    if email is None or email is False:
        return jsonify({"error": "Verify your email to view order history"}), 401
    with get_connection() as connection:
        orders = connection.execute(
            """
            SELECT orders.*, users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            WHERE lower(trim(orders.customer_email)) = ?
            ORDER BY orders.created_at DESC, orders.id DESC
            """,
            (email,),
        ).fetchall()
    return jsonify([public_order(order) for order in orders])


@app.post("/api/orders")
@limiter.limit("10 per minute")
def create_order():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid order payload is required"}), 400
    supplied_name = payload.get("customer_name")
    customer_name = supplied_name.strip() if isinstance(supplied_name, str) else ""
    customer_name = customer_name or "Guest customer"
    supplied_email = payload.get("customer_email")
    customer_email = supplied_email.strip().lower() if isinstance(supplied_email, str) else ""
    if not customer_email:
        return jsonify({"error": "Enter an email address so you can access your order history"}), 400
    order_type = str(payload.get("order_type", "Dine in")).strip()
    payment_method_value = payload.get("payment_method", "cash")
    if not isinstance(payment_method_value, str):
        return jsonify({"error": "Choose cash or card as the payment method"}), 400
    payment_method = payment_method_value.strip().lower()
    if payment_method == "online":
        return jsonify({"error": "Online payment is not available until Stripe checkout is configured."}), 503
    if payment_method not in {"cash", "card"}:
        return jsonify({"error": "Choose cash or card as the payment method"}), 400
    table_number = payload.get("table_number")
    items = payload.get("items")

    if len(customer_name) > 160:
        return jsonify({"error": "Customer names must be 160 characters or fewer"}), 400
    if len(customer_email) > 254 or not EMAIL_PATTERN.fullmatch(customer_email):
        return jsonify({"error": "Enter a valid email address"}), 400
    if not isinstance(items, list) or not 1 <= len(items) <= 50:
        return jsonify({"error": "At least one item is required"}), 400
    if order_type not in VALID_ORDER_TYPES:
        return jsonify({"error": "Choose Dine in or Takeaway"}), 400
    if order_type == "Delivery":
        return jsonify({"error": "We currently don't offer delivery"}), 400
    if order_type == "Dine in":
        table_number = str(table_number or "").strip().upper()
        if table_number not in TABLE_NUMBERS:
            return jsonify({"error": "Choose an available table for dine-in"}), 400
    elif table_number is not None:
        return jsonify({"error": "A table can only be selected for dine-in"}), 400
    clean_items = []
    total = 0
    current_time = datetime.now().astimezone().replace(tzinfo=None).isoformat(timespec="minutes")
    for item in items:
        if not isinstance(item, dict):
            return jsonify({"error": "Each order item must have a valid price and quantity"}), 400
        name = str(item.get("name", "")).strip()
        quantity = item.get("quantity")
        if not name or len(name) > 120 or isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 20:
            return jsonify({"error": "Each order item must have a valid menu item and quantity"}), 400
        with get_connection() as connection:
            menu_item = connection.execute(
                "SELECT id, name, price FROM menu_items WHERE name = ? COLLATE NOCASE AND is_public = 1",
                (name,),
            ).fetchone()
            if not menu_item:
                return jsonify({"error": "One or more items are not on the current menu"}), 400
            name = menu_item["name"]
            price = menu_item["price"]
            promotion = connection.execute(
                """
                SELECT menu_items.price, promotions.discount_percent
                FROM menu_items
                JOIN promotions ON promotions.menu_item_id = menu_items.id
                WHERE menu_items.name = ? AND promotions.start_at <= ? AND promotions.end_at >= ?
                ORDER BY promotions.discount_percent DESC, promotions.created_at DESC
                LIMIT 1
                """,
                (name, current_time, current_time),
            ).fetchone()
        if promotion:
            price = round(promotion["price"] * (100 - promotion["discount_percent"]) / 100, 2)
        clean_items.append({"name": name, "price": price, "quantity": quantity})
        total += price * quantity
    if not math.isfinite(total):
        return jsonify({"error": "The order total is invalid"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    tracking_token = secrets.token_urlsafe(32)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if table_number:
            occupied = connection.execute(
                """
                SELECT 1 FROM orders
                WHERE table_number = ? AND status NOT IN ('Paid', 'Cancelled')
                LIMIT 1
                """,
                (table_number,),
            ).fetchone()
            reservation = connection.execute(
                """
                SELECT 1 FROM order_table_reservations
                WHERE table_number = ? AND status != 'Cancelled'
                  AND starts_at <= ? AND ends_at > ?
                LIMIT 1
                """,
                (table_number, timestamp, timestamp),
            ).fetchone()
            if occupied or reservation:
                return jsonify({"error": "That table is no longer available. Choose another table."}), 409
        cursor = connection.execute(
            """
            INSERT INTO orders
                (customer_name, customer_email, items, total, status, order_type, payment_method,
                 table_number, tracking_token_hash, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'Received', ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_name,
                customer_email,
                json.dumps(clean_items),
                total,
                order_type,
                payment_method,
                table_number,
                tracking_token_hash(tracking_token),
                timestamp,
                timestamp,
            ),
        )
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)).fetchone()
        connection.execute("UPDATE orders SET order_number = ? WHERE id = ?", (f"BDU-{order['id']:05d}", order['id']))
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (cursor.lastrowid,)).fetchone()

    result = public_order(order)
    result["tracking_token"] = tracking_token
    return jsonify(result), 201


@app.get("/api/orders")
@require_role("waiter", "manager")
def list_orders():
    with get_connection() as connection:
        active = request.args.get('active', '').lower() == 'true'
        where = "WHERE orders.status NOT IN ('Completed', 'Paid', 'Cancelled')" if active else ""
        if active:
            where = "WHERE orders.status NOT IN ('Completed', 'Paid', 'Cancelled')"
        orders = connection.execute(
            f"""
            SELECT orders.*, users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            {where}
            ORDER BY orders.created_at DESC
            """
        ).fetchall()
    return jsonify([public_order(order) for order in orders])


@app.get("/api/orders/<int:order_id>")
def get_order(order_id):
    with get_connection() as connection:
        order = connection.execute(
            """
            SELECT orders.*, users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            WHERE orders.id = ?
            """,
            (order_id,),
        ).fetchone()
    token = request.headers.get("X-Order-Tracking-Token", "")
    history_email = customer_order_history_email()
    if not order or not (
        order_tracking_is_valid(order, token)
        or isinstance(history_email, str)
        and history_email == str(order["customer_email"]).strip().lower()
    ):
        return jsonify({"error": "Order not found"}), 404
    return jsonify(public_order(order))


@app.post("/api/orders/<int:order_id>/rating")
@limiter.limit("5 per hour")
def rate_order(order_id):
    payload = request.get_json(silent=True) or {}
    rating = payload.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int):
        return jsonify({"error": "Choose a rating from 1 to 5"}), 400
    if rating < 1 or rating > 5:
        return jsonify({"error": "Choose a rating from 1 to 5"}), 400

    with get_connection() as connection:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        token = request.headers.get("X-Order-Tracking-Token", "")
        if not order or not order_tracking_is_valid(order, token):
            return jsonify({"error": "Order not found"}), 404
        if order["status"] not in {"Completed", "Paid"}:
            return jsonify({"error": "You can rate an order after it is completed"}), 409
        if order["rating"] is not None:
            return jsonify({"error": "This order has already been rated"}), 409
        cursor = connection.execute(
            "UPDATE orders SET rating = ? WHERE id = ? AND status IN ('Completed', 'Paid') AND rating IS NULL",
            (rating, order_id),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "This order has already been rated"}), 409
        updated_order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()

    return jsonify(public_order(updated_order))


@app.patch("/api/orders/<int:order_id>")
@require_role("waiter", "manager")
def update_order(order_id):
    payload = request.get_json(silent=True) or {}
    status = payload.get("status")
    if status not in VALID_ORDER_STATUSES:
        return jsonify({"error": "Choose a valid order status"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        current_order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not current_order:
            return jsonify({"error": "Order not found"}), 404
        if status == "Served" and current_order["order_type"] != "Dine in":
            return jsonify({"error": "Only dine-in orders can be marked served"}), 400
        if status == "Completed" and current_order["order_type"] == "Dine in":
            return jsonify({"error": "Dine-in orders are completed when the waiter confirms payment"}), 409
        can_record_payment = (
            current_order["order_type"] == "Dine in"
            and current_order["status"] == "Served"
        ) or (
            current_order["order_type"] == "Takeaway"
            and current_order["status"] == "Ready"
        )
        if status == "Paid" and (
            not can_record_payment
            or current_order["payment_method"] not in {"cash", "card"}
        ):
            return jsonify({"error": "Cash or card payment can be recorded after a dine-in order is served or a takeaway order is ready"}), 409
        cursor = connection.execute(
            """
            UPDATE orders SET status = ?, updated_at = ?,
                waiter_id = COALESCE(waiter_id, ?)
            WHERE id = ?
            """,
            (status, timestamp, g.current_user["id"], order_id),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Order not found"}), 404
        order = connection.execute(
            """
            SELECT orders.*, users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            WHERE orders.id = ?
            """,
            (order_id,),
        ).fetchone()
    return jsonify(public_order(order))


@app.patch("/api/orders/<int:order_id>/claim")
@require_role("waiter", "manager")
def claim_order(order_id):
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE orders SET waiter_id = ?, updated_at = ?
            WHERE id = ? AND status NOT IN ('Paid', 'Completed', 'Cancelled')
              AND (waiter_id IS NULL OR waiter_id = ?)
            """,
            (g.current_user["id"], timestamp, order_id, g.current_user["id"]),
        )
        if cursor.rowcount == 0:
            order = connection.execute(
                "SELECT waiter_id, status FROM orders WHERE id = ?", (order_id,)
            ).fetchone()
            if not order:
                return jsonify({"error": "Order not found"}), 404
            if order["waiter_id"] and order["waiter_id"] != g.current_user["id"]:
                return jsonify({"error": "This order is assigned to another staff member"}), 409
            return jsonify({"error": "This order is no longer active"}), 409
        order = connection.execute(
            """
            SELECT orders.*, users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            WHERE orders.id = ?
            """,
            (order_id,),
        ).fetchone()
    return jsonify(public_order(order))


@app.post("/api/orders/<int:order_id>/request-payment")
@limiter.limit("5 per minute")
def request_order_payment(order_id):
    token = request.headers.get("X-Order-Tracking-Token", "")
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        history_email = customer_order_history_email()
        if not order or not (
            order_tracking_is_valid(order, token)
            or isinstance(history_email, str)
            and history_email == str(order["customer_email"]).strip().lower()
        ):
            return jsonify({"error": "Order not found"}), 404
        if (
            order["order_type"] != "Dine in"
            or order["status"] != "Served"
            or order["payment_method"] not in {"cash", "card"}
        ):
            return jsonify({"error": "You can request payment after your dine-in order has been served"}), 409
        connection.execute(
            "UPDATE orders SET payment_requested = 1, updated_at = ? WHERE id = ?",
            (timestamp, order_id),
        )
        updated = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    return jsonify(public_order(updated))


@app.get("/api/public/tables/available")
def public_available_tables():
    requested_start = request.args.get("starts_at")
    if requested_start:
        starts_at = parse_iso_datetime(requested_start)
        if not starts_at:
            return jsonify({"error": "Choose a valid reservation time"}), 400
    else:
        starts_at = datetime.now(timezone.utc)
    ends_at = starts_at + timedelta(minutes=RESERVATION_DURATION_MINUTES)
    with get_connection() as connection:
        tables = available_tables_for_period(connection, starts_at, ends_at)
    return jsonify(tables)


@app.post("/api/orders/<int:order_id>/reservations")
@limiter.limit("5 per hour")
def create_reservation(order_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid reservation is required"}), 400
    starts_at = parse_iso_datetime(payload.get("starts_at"))
    table_number = str(payload.get("table_number", "")).strip().upper()
    if (
        not starts_at
        or starts_at <= datetime.now(timezone.utc)
        or starts_at.second != 0
        or starts_at.microsecond != 0
        or starts_at.astimezone(SOUTH_AFRICA_TIMEZONE).minute not in {0, 30}
    ):
        return jsonify({"error": "Choose a future reservation time on the hour or half hour"}), 400
    if table_number not in TABLE_NUMBERS:
        return jsonify({"error": "Choose an available table"}), 400
    ends_at = starts_at + timedelta(minutes=RESERVATION_DURATION_MINUTES)
    starts_text = starts_at.isoformat(timespec="seconds")
    ends_text = ends_at.isoformat(timespec="seconds")
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        token = str(payload.get("tracking_token", ""))
        if not order or not order_tracking_is_valid(order, token):
            return jsonify({"error": "Order not found"}), 404
        existing = connection.execute(
            "SELECT id FROM order_table_reservations WHERE order_id = ? AND status != 'Cancelled'",
            (order_id,),
        ).fetchone()
        if existing:
            return jsonify({"error": "This order already has a table reservation"}), 409
        available = available_tables_for_period(connection, starts_at, ends_at)
        if table_number not in available:
            return jsonify({"error": "That table is not available for the selected time"}), 409
        cursor = connection.execute(
            """
            INSERT INTO order_table_reservations
                (order_id, table_number, starts_at, ends_at, fee_amount,
                 fee_paid, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 0, 'Booked', ?, ?)
            """,
            (
                order_id,
                table_number,
                starts_text,
                ends_text,
                RESERVATION_FEE,
                timestamp,
                timestamp,
            ),
        )
        reservation = connection.execute(
            """
            SELECT order_table_reservations.*, orders.order_number
            FROM order_table_reservations JOIN orders ON orders.id = order_table_reservations.order_id
            WHERE order_table_reservations.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
    return jsonify(dict(reservation)), 201


@app.get("/api/orders/<int:order_id>/reservation")
def get_customer_reservation(order_id):
    token = request.headers.get("X-Order-Tracking-Token", "")
    with get_connection() as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not order or not order_tracking_is_valid(order, token):
            return jsonify({"error": "Order not found"}), 404
        reservation = connection.execute(
            "SELECT * FROM order_table_reservations WHERE order_id = ? AND status != 'Cancelled' ORDER BY id DESC LIMIT 1",
            (order_id,),
        ).fetchone()
    return jsonify({"reservation": dict(reservation) if reservation else None})


@app.get("/api/reservations")
@require_role("waiter", "manager")
def list_reservations():
    with get_connection() as connection:
        reservations = connection.execute(
            """
            SELECT order_table_reservations.*, orders.order_number, orders.customer_name,
                   orders.customer_email, orders.waiter_id, orders.status AS order_status,
                   orders.created_at AS order_created_at,
                   users.firstname AS waiter_firstname, users.lastname AS waiter_lastname
            FROM order_table_reservations
            JOIN orders ON orders.id = order_table_reservations.order_id
            LEFT JOIN users ON users.id = orders.waiter_id
            ORDER BY order_table_reservations.starts_at DESC
            """
        ).fetchall()
        legacy_reservations = connection.execute(
            "SELECT * FROM reservations ORDER BY reservation_at DESC"
        ).fetchall()
    result = [dict(reservation) for reservation in reservations]
    for reservation in legacy_reservations:
        try:
            starts_at = datetime.fromisoformat(reservation["reservation_at"])
            ends_at = starts_at + timedelta(minutes=reservation["duration_minutes"])
            starts_text = starts_at.isoformat()
            ends_text = ends_at.isoformat()
        except (TypeError, ValueError):
            starts_text = reservation["reservation_at"]
            ends_text = reservation["reservation_at"]
        result.append({
            "id": f'legacy-{reservation["id"]}',
            "order_id": None,
            "order_number": "Existing booking",
            "customer_name": reservation["guest_name"],
            "customer_email": reservation["guest_email"],
            "table_number": reservation["table_number"],
            "starts_at": starts_text,
            "ends_at": ends_text,
            "fee_amount": None,
            "fee_paid": True,
            "status": reservation["status"],
            "waiter_firstname": None,
            "waiter_lastname": None,
        })
    return jsonify(sorted(result, key=lambda item: item.get("starts_at") or "", reverse=True))


@app.patch("/api/reservations/<int:reservation_id>/fee")
@require_role("waiter", "manager")
def update_reservation_fee(reservation_id):
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE order_table_reservations SET fee_paid = 1, updated_at = ?
            WHERE id = ? AND status = 'Booked' AND fee_paid = 0
            """,
            (timestamp, reservation_id),
        )
        if cursor.rowcount == 0:
            reservation = connection.execute(
                "SELECT * FROM order_table_reservations WHERE id = ?", (reservation_id,)
            ).fetchone()
            if not reservation:
                return jsonify({"error": "Reservation not found"}), 404
            if reservation["fee_paid"]:
                return jsonify({"error": "The reservation fee is already paid"}), 409
            return jsonify({"error": "Only booked reservations can be paid"}), 409
        reservation = connection.execute(
            "SELECT * FROM order_table_reservations WHERE id = ?", (reservation_id,)
        ).fetchone()
    return jsonify(dict(reservation))


@app.get("/api/tables/available")
@require_role("waiter", "manager")
def available_tables():
    now = datetime.now(timezone.utc)
    with get_connection() as connection:
        tables = available_tables_for_period(
            connection, now, now + timedelta(minutes=RESERVATION_DURATION_MINUTES)
        )
    return jsonify(tables)


@app.patch("/api/orders/<int:order_id>/table")
@require_role("waiter", "manager")
def assign_order_table(order_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid table number is required"}), 400
    table_number = str(payload.get("table_number", "")).strip().upper()
    if table_number not in TABLE_NUMBERS:
        return jsonify({"error": "That table does not exist"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if order["order_type"] != "Dine in":
            return jsonify({"error": "Only dine-in orders can be assigned a table"}), 400
        occupied = connection.execute(
            "SELECT id FROM orders WHERE table_number = ? AND id != ? AND status NOT IN ('Paid', 'Cancelled')",
            (table_number, order_id),
        ).fetchone()
        now = datetime.now(timezone.utc)
        reservation = connection.execute(
            """
            SELECT id FROM order_table_reservations
            WHERE table_number = ? AND status != 'Cancelled'
              AND starts_at <= ? AND ends_at > ?
            LIMIT 1
            """,
            (table_number, now.isoformat(), now.isoformat()),
        ).fetchone()
        if occupied or reservation:
            return jsonify({"error": "That table is currently occupied"}), 409
        connection.execute(
            """
            UPDATE orders SET table_number = ?, updated_at = ?,
                waiter_id = COALESCE(waiter_id, ?)
            WHERE id = ?
            """,
            (table_number, timestamp, g.current_user["id"], order_id),
        )
        updated = connection.execute(
            """
            SELECT orders.*, users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            WHERE orders.id = ?
            """,
            (order_id,),
        ).fetchone()
    return jsonify(public_order(updated))


@app.get("/")
def restaurant_home():
    return send_from_directory(FRONTEND_DIST_PATH, "index.html")


@app.get("/assets/<path:filename>")
def restaurant_assets(filename):
    return send_from_directory(FRONTEND_DIST_PATH / "assets", filename)


@app.get("/dashboard/")
@app.get("/dashboard/<section>")
def dashboard(section=None):
    """Render the staff dashboard with the given section"""
    valid_sections = ["service", "orders", "tables", "menu", "pantry", "shifts", "complaints"]
    if section is None:
        current_section = "complaints"
    else:
        current_section = section.lower() if section in valid_sections else "complaints"
    return render_template("dashboard.html", current_section=current_section)


@app.get("/api/pantry")
@require_role("manager")
def list_pantry_items():
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM pantry_items ORDER BY name COLLATE NOCASE").fetchall()
    items = [pantry_item_to_dict(row) for row in rows]
    today = datetime.now(timezone.utc).date()
    return jsonify({
        "items": items,
        "summary": {
            "total_items": len(items),
            "low_stock": sum(item["status"] == "Low Stock" for item in items),
            "out_of_stock": sum(item["quantity"] <= 0 for item in items),
            "expiring_soon": sum(
                item["expiry_date"] is not None
                and today <= datetime.strptime(item["expiry_date"], "%Y-%m-%d").date()
                <= today + timedelta(days=7)
                for item in items
            ),
        },
    })


def validate_pantry_payload(payload, existing=None):
    if not isinstance(payload, dict):
        raise ValueError("A valid pantry item is required")
    current = dict(existing or {})
    name = str(payload.get("name", current.get("name", ""))).strip()
    category = str(payload.get("category", current.get("category", "Produce"))).strip()
    unit = str(payload.get("unit", current.get("unit", "kg"))).strip()
    supplier = str(payload.get("supplier", current.get("supplier", ""))).strip()
    expiry_date = payload.get("expiry_date", current.get("expiry_date"))
    if not 1 <= len(name) <= 120:
        raise ValueError("Item name is required and must be 120 characters or fewer")
    if category not in PANTRY_CATEGORIES:
        raise ValueError("Choose a valid pantry category")
    if unit not in PANTRY_UNITS:
        raise ValueError("Choose a valid unit")
    if len(supplier) > 120:
        raise ValueError("Supplier must be 120 characters or fewer")
    if expiry_date == "":
        expiry_date = None
    if expiry_date is not None:
        try:
            expiry_date = datetime.strptime(str(expiry_date), "%Y-%m-%d").date().isoformat()
        except ValueError:
            raise ValueError("Expiry date must be a valid date") from None
    quantity = pantry_number(payload.get("quantity", current.get("quantity", 0)), "Quantity")
    reorder_level = pantry_number(
        payload.get("reorder_level", current.get("reorder_level", 0)), "Reorder level"
    )
    cost_per_unit = pantry_number(
        payload.get("cost_per_unit", current.get("cost_per_unit", 0)), "Cost per unit"
    )
    return {
        "name": name,
        "category": category,
        "quantity": quantity,
        "unit": unit,
        "reorder_level": reorder_level,
        "cost_per_unit": cost_per_unit,
        "supplier": supplier,
        "expiry_date": expiry_date,
    }


@app.post("/api/pantry")
@require_role("manager")
def create_pantry_item():
    try:
        item = validate_pantry_payload(request.get_json(silent=True))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            INSERT INTO pantry_items
                (name, category, quantity, unit, reorder_level, cost_per_unit, supplier, expiry_date, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*item.values(), timestamp),
        )
        if item["quantity"] > 0:
            actor = g.current_user
            connection.execute(
                """
                INSERT INTO stock_movements
                    (item_id, item_name, quantity_change, reason, supplier, cost_per_unit,
                     movement_date, performed_by, performed_by_name)
                VALUES (?, ?, ?, 'Initial stock', ?, ?, ?, ?, ?)
                """,
                (
                    cursor.lastrowid,
                    item["name"],
                    item["quantity"],
                    item["supplier"],
                    item["cost_per_unit"],
                    datetime.now(timezone.utc).date().isoformat(),
                    actor["id"],
                    f"{actor['firstname']} {actor['lastname']}",
                ),
            )
        row = connection.execute(
            "SELECT * FROM pantry_items WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
    return jsonify(pantry_item_to_dict(row)), 201


@app.patch("/api/pantry/<int:item_id>")
@require_role("manager")
def update_pantry_item(item_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid pantry item update is required"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        row = connection.execute("SELECT * FROM pantry_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return jsonify({"error": "Pantry item not found"}), 404
        try:
            item = validate_pantry_payload(payload, row)
        except ValueError as error:
            return jsonify({"error": str(error)}), 400
        connection.execute(
            """
            UPDATE pantry_items
            SET name = ?, category = ?, quantity = ?, unit = ?, reorder_level = ?,
                cost_per_unit = ?, supplier = ?, expiry_date = ?, last_updated = ?
            WHERE id = ?
            """,
            (*item.values(), timestamp, item_id),
        )
        if item["quantity"] != row["quantity"]:
            actor = g.current_user
            connection.execute(
                """
                INSERT INTO stock_movements
                    (item_id, item_name, quantity_change, reason, supplier, cost_per_unit,
                     movement_date, performed_by, performed_by_name)
                VALUES (?, ?, ?, 'Manual edit', '', NULL, ?, ?, ?)
                """,
                (
                    item_id,
                    item["name"],
                    item["quantity"] - row["quantity"],
                    datetime.now(timezone.utc).date().isoformat(),
                    actor["id"],
                    f"{actor['firstname']} {actor['lastname']}",
                ),
            )
        updated = connection.execute(
            "SELECT * FROM pantry_items WHERE id = ?", (item_id,)
        ).fetchone()
    return jsonify(pantry_item_to_dict(updated))


@app.delete("/api/pantry/<int:item_id>")
@require_role("manager")
def delete_pantry_item(item_id):
    with get_connection() as connection:
        cursor = connection.execute("DELETE FROM pantry_items WHERE id = ?", (item_id,))
    if cursor.rowcount == 0:
        return jsonify({"error": "Pantry item not found"}), 404
    return jsonify({"message": "Pantry item deleted"})


def movement_to_dict(row):
    return dict(row)


@app.get("/api/pantry/movements")
@require_role("manager")
def list_stock_movements():
    with get_connection() as connection:
        rows = connection.execute(
            "SELECT * FROM stock_movements ORDER BY movement_date DESC, id DESC LIMIT 500"
        ).fetchall()
    return jsonify([movement_to_dict(row) for row in rows])


@app.post("/api/pantry/<int:item_id>/restock")
@require_role("manager")
def restock_pantry_item(item_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid restock is required"}), 400
    try:
        received = pantry_number(payload.get("quantity_received"), "Quantity received")
        if received <= 0:
            raise ValueError("Quantity received must be greater than 0")
        supplier = str(payload.get("supplier", "")).strip()
        if not supplier or len(supplier) > 120:
            raise ValueError("Supplier is required and must be 120 characters or fewer")
        cost = pantry_number(payload.get("cost_per_unit"), "Cost per unit")
        try:
            movement_date = datetime.strptime(
                str(payload.get("date", "")), "%Y-%m-%d"
            ).date().isoformat()
        except ValueError:
            raise ValueError("Movement date must be a valid date") from None
    except ValueError as error:
        return jsonify({"error": str(error) or "Enter a valid restock date"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    actor = g.current_user
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute(
            "SELECT * FROM pantry_items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            return jsonify({"error": "Pantry item not found"}), 404
        connection.execute(
            "UPDATE pantry_items SET quantity = quantity + ?, supplier = ?, cost_per_unit = ?, last_updated = ? WHERE id = ?",
            (received, supplier, cost, timestamp, item_id),
        )
        connection.execute(
            """
            INSERT INTO stock_movements
                (item_id, item_name, quantity_change, reason, supplier, cost_per_unit,
                 movement_date, performed_by, performed_by_name)
            VALUES (?, ?, ?, 'Restock', ?, ?, ?, ?, ?)
            """,
            (item_id, item["name"], received, supplier, cost, movement_date,
             actor["id"], f"{actor['firstname']} {actor['lastname']}"),
        )
        updated = connection.execute(
            "SELECT * FROM pantry_items WHERE id = ?", (item_id,)
        ).fetchone()
    return jsonify(pantry_item_to_dict(updated))


@app.post("/api/pantry/<int:item_id>/adjust")
@require_role("manager")
def adjust_pantry_item(item_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid stock adjustment is required"}), 400
    try:
        quantity = pantry_number(payload.get("quantity"), "Quantity")
        if quantity <= 0:
            raise ValueError("Quantity must be greater than 0")
        reason = str(payload.get("reason", "")).strip()
        if reason not in PANTRY_MOVEMENT_REASONS:
            raise ValueError("Choose Usage, Wastage or Spoilage as the reason")
        try:
            movement_date = datetime.strptime(
                str(payload.get("date", "")), "%Y-%m-%d"
            ).date().isoformat()
        except ValueError:
            raise ValueError("Movement date must be a valid date") from None
    except ValueError as error:
        return jsonify({"error": str(error) or "Enter a valid adjustment date"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    actor = g.current_user
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        item = connection.execute(
            "SELECT * FROM pantry_items WHERE id = ?", (item_id,)
        ).fetchone()
        if not item:
            return jsonify({"error": "Pantry item not found"}), 404
        if quantity > item["quantity"]:
            return jsonify({"error": "Adjustment cannot exceed the quantity on hand"}), 400
        connection.execute(
            "UPDATE pantry_items SET quantity = quantity - ?, last_updated = ? WHERE id = ?",
            (quantity, timestamp, item_id),
        )
        connection.execute(
            """
            INSERT INTO stock_movements
                (item_id, item_name, quantity_change, reason, supplier, cost_per_unit,
                 movement_date, performed_by, performed_by_name)
            VALUES (?, ?, ?, ?, '', NULL, ?, ?, ?)
            """,
            (item_id, item["name"], -quantity, reason, movement_date,
             actor["id"], f"{actor['firstname']} {actor['lastname']}"),
        )
        updated = connection.execute(
            "SELECT * FROM pantry_items WHERE id = ?", (item_id,)
        ).fetchone()
    return jsonify(pantry_item_to_dict(updated))


@app.get("/api/complaints")
@require_role("waiter", "manager")
def list_complaints():
    status = request.args.get("status")
    query = """SELECT complaints.*, orders.order_number, orders.created_at AS order_created_at,
               orders.items, orders.total, orders.status AS order_status,
               waiter.firstname AS waiter_firstname, waiter.lastname AS waiter_lastname
               FROM complaints LEFT JOIN orders ON orders.id = complaints.order_id
               LEFT JOIN users AS waiter ON waiter.id = complaints.waiter_id"""
    parameters = []

    if status in VALID_STATUSES:
        query += " WHERE complaints.status = ?"
        parameters.append(status)

    query += " ORDER BY created_at DESC"
    with get_connection() as connection:
        complaints = connection.execute(query, parameters).fetchall()

    return jsonify([complaint_to_dict(complaint) for complaint in complaints])


def export_pdf_bytes(sections, title="DineDesk company records"):
    lines = [title, datetime.now(SOUTH_AFRICA_TIMEZONE).strftime("%Y-%m-%d %H:%M")]
    for name, rows in sections.items():
        lines.extend(["", f"{name.replace('_', ' ').title()} | {len(rows)} records"])
        lines.append("-" * 72)
        if not rows:
            lines.append("No records.")
            continue
        for index, row in enumerate(rows, 1):
            serialized = json.dumps(row, ensure_ascii=True, default=str, sort_keys=True)
            lines.extend(textwrap.wrap(
                f"{index}. {serialized}",
                width=112,
                subsequent_indent="   ",
                break_long_words=True,
                break_on_hyphens=False,
            ))

    pages = [lines[index:index + 58] for index in range(0, len(lines), 58)] or [[]]
    objects = [None, b"<< /Type /Catalog /Pages 2 0 R >>", None, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    page_references = []
    for page_lines in pages:
        page_number = len(objects)
        content_number = page_number + 1
        page_references.append(f"{page_number} 0 R")
        content = ["BT", "/F1 8 Tf", "42 800 Td", "11 TL"]
        for line in page_lines:
            ascii_line = line.encode("ascii", "replace").decode("ascii")
            escaped_line = ascii_line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            content.append(f"({escaped_line}) Tj")
            content.append("T*")
        content.append("ET")
        stream = "\n".join(content).encode("ascii")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 842] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n"
            + stream + b"\nendstream"
        )
    objects[2] = (
        f"<< /Type /Pages /Kids [{' '.join(page_references)}] /Count {len(page_references)} >>"
    ).encode("ascii")
    output = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for object_number, body in enumerate(objects[1:], 1):
        offsets.append(len(output))
        output.extend(f"{object_number} 0 obj\n".encode("ascii"))
        output.extend(body)
        output.extend(b"\nendobj\n")
    xref_offset = len(output)
    output.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    output.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(output)


@app.get("/api/exports/<dataset>")
@require_role("manager")
def export_manager_data(dataset):
    if dataset == "weekly-roster":
        week_start_value = request.args.get("week_start")
        try:
            week_start = datetime.strptime(week_start_value or "", "%Y-%m-%d").date()
        except ValueError:
            return jsonify({"error": "Week start must use YYYY-MM-DD format"}), 400
        week_end = week_start + timedelta(days=7)
        try:
            with get_connection() as connection:
                rows = connection.execute(
                    """
                    SELECT staff_roster_assignments.work_date,
                           staff_roster.firstname || ' ' || staff_roster.lastname AS staff_member,
                           staff_roster.role, staff_roster_assignments.shift_label,
                           staff_roster_assignments.starts_at, staff_roster_assignments.ends_at
                    FROM staff_roster_assignments
                    JOIN staff_roster ON staff_roster.id = staff_roster_assignments.staff_id
                    WHERE staff_roster_assignments.work_date >= ?
                      AND staff_roster_assignments.work_date < ?
                    ORDER BY staff_roster_assignments.work_date,
                             staff_roster_assignments.starts_at,
                             staff_roster.firstname COLLATE NOCASE,
                             staff_roster.lastname COLLATE NOCASE
                    """,
                    (week_start.isoformat(), week_end.isoformat()),
                ).fetchall()
        except DATABASE_ERRORS:
            app.logger.exception("Could not export weekly roster starting %s", week_start)
            return jsonify({"error": "Could not export the weekly roster"}), 500

        filename = f"dinedesk-weekly-roster-{week_start:%Y%m%d}.pdf"
        title = f"DineDesk weekly roster: {week_start} to {week_end - timedelta(days=1)}"
        return Response(
            export_pdf_bytes({"Weekly roster": [dict(row) for row in rows]}, title),
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    queries = {
        "orders": """
            SELECT orders.id, orders.order_number, orders.customer_name, orders.customer_email,
                   orders.items, orders.total, orders.status, orders.order_type,
                   orders.payment_method, orders.table_number, orders.payment_requested,
                   orders.rating, orders.created_at, orders.updated_at,
                   users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            ORDER BY orders.created_at
        """,
        "pantry": "SELECT * FROM pantry_items ORDER BY id",
        "stock_movements": "SELECT * FROM stock_movements ORDER BY movement_date",
        "staff": """
            SELECT id, user_id, firstname, lastname, email, role, hourly_rate, created_at
            FROM staff_roster ORDER BY id
        """,
        "shifts": "SELECT * FROM shifts ORDER BY created_at",
        "roster": """
            SELECT staff_roster_assignments.id, staff_roster.firstname, staff_roster.lastname,
                   staff_roster.email, staff_roster.role, staff_roster.hourly_rate,
                   staff_roster_assignments.work_date, staff_roster_assignments.shift_label,
                   staff_roster_assignments.starts_at, staff_roster_assignments.ends_at
            FROM staff_roster_assignments
            JOIN staff_roster ON staff_roster.id = staff_roster_assignments.staff_id
            ORDER BY staff_roster_assignments.work_date, staff_roster_assignments.starts_at
        """,
        "attendance": """
            SELECT attendance_records.id, users.firstname, users.lastname, users.email,
                   users.role, attendance_records.clock_in_at, attendance_records.clock_out_at
            FROM attendance_records JOIN users ON users.id = attendance_records.user_id
            ORDER BY attendance_records.clock_in_at
        """,
        "complaints": """
            SELECT id, customer_name, customer_email, message, status, created_at, updated_at,
                   order_id, order_number, category, severity, description, reference,
                   identity_mode, manager_response, responded_at, resolved_at, email_status
            FROM complaints ORDER BY created_at
        """,
        "reservations": """
            SELECT order_table_reservations.id, 'Order reservation' AS source,
                   order_table_reservations.order_id, orders.order_number,
                   orders.customer_name, orders.customer_email, NULL AS customer_phone,
                   order_table_reservations.table_number, order_table_reservations.starts_at,
                   order_table_reservations.ends_at, order_table_reservations.fee_amount,
                   order_table_reservations.fee_paid, order_table_reservations.status,
                   order_table_reservations.created_at,
                   waiter.firstname || ' ' || waiter.lastname AS waiter_name
            FROM order_table_reservations
            JOIN orders ON orders.id = order_table_reservations.order_id
            LEFT JOIN users AS waiter ON waiter.id = orders.waiter_id
            UNION ALL
            SELECT reservations.id, 'Existing booking' AS source,
                   NULL AS order_id, NULL AS order_number,
                   reservations.guest_name AS customer_name, reservations.guest_email AS customer_email,
                   reservations.guest_phone AS customer_phone, reservations.table_number,
                   reservations.reservation_at AS starts_at,
                   datetime(reservations.reservation_at, '+' || reservations.duration_minutes || ' minutes') AS ends_at,
                   NULL AS fee_amount, NULL AS fee_paid, reservations.status, reservations.created_at,
                   NULL AS waiter_name
            FROM reservations
            ORDER BY starts_at
        """,
        "menu": "SELECT id, name, category, description, price, icon, image FROM menu_items WHERE is_public = 1 ORDER BY id",
        "promotions": "SELECT * FROM promotions ORDER BY created_at",
        "waitlist": "SELECT * FROM waitlist ORDER BY created_at",
    }
    datasets = {
        "orders": ["orders"],
        "pantry": ["pantry", "stock_movements"],
        "staff": ["staff", "shifts", "roster", "attendance"],
        "complaints": ["complaints"],
        "reservations": ["reservations"],
        "waitlist": ["waitlist"],
        "all": list(queries),
    }
    if dataset not in datasets:
        return jsonify({"error": "Choose orders, pantry, staff, weekly-roster, complaints, reservations, waitlist, or all"}), 404
    try:
        with get_connection() as connection:
            exported = {}
            for name in datasets[dataset]:
                cursor = connection.execute(queries[name])
                exported[name] = [dict(row) for row in cursor.fetchall()]
    except DATABASE_ERRORS:
        app.logger.exception("Could not export manager dataset %s", dataset)
        return jsonify({"error": "Could not export company records"}), 500

    filename = f"dinedesk-{dataset}-{datetime.now(SOUTH_AFRICA_TIMEZONE):%Y%m%d}.pdf"
    return Response(
        export_pdf_bytes(exported),
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/complaints")
@limiter.limit("5 per hour")
def create_complaint():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid complaint payload is required"}), 400
    try:
        order_id = int(payload.get("order_id"))
    except (TypeError, ValueError):
        return jsonify({"error": "A valid order is required"}), 400
    category = str(payload.get("category", "")).strip()
    severity = str(payload.get("severity", "Problem with my order")).strip()
    description = str(payload.get("description", "")).strip()
    identity_mode = str(payload.get("identity_mode", "anonymous")).strip().lower()
    customer_email = str(payload.get("customer_email", "")).strip().lower()
    if category not in VALID_CATEGORIES or severity not in VALID_SEVERITIES or not 10 <= len(description) <= 2000:
        return jsonify({"error": "Choose a valid category and severity, and describe the issue in 10 to 2000 characters"}), 400
    if identity_mode not in {"anonymous", "add_email"}:
        return jsonify({"error": "Choose Anonymous or Add email"}), 400
    if identity_mode == "add_email" and (
        len(customer_email) > 254 or not EMAIL_PATTERN.fullmatch(customer_email)
    ):
        return jsonify({"error": "Enter a valid email address or choose Anonymous"}), 400
    if identity_mode == "anonymous":
        customer_email = "Not provided"

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        order = connection.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        tracking_token = str(payload.get("tracking_token", ""))
        if not order or not order_tracking_is_valid(order, tracking_token):
            return jsonify({"error": "Order not found"}), 404
        waiter = connection.execute("SELECT id FROM users WHERE role = 'waiter' ORDER BY id LIMIT 1").fetchone()
        manager = connection.execute("SELECT id FROM users WHERE role = 'manager' ORDER BY id LIMIT 1").fetchone()
        reference = f"CMP-{datetime.now(timezone.utc).year}-{order_id:05d}"
        cursor = connection.execute(
            """
            INSERT INTO complaints
                (customer_name, customer_email, message, status, created_at, updated_at,
                  order_id, order_number, category, severity, description, waiter_id, manager_id, reference, identity_mode)
              VALUES (?, ?, ?, 'Open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (order["customer_name"], customer_email, description, timestamp, timestamp,
             order_id, order["order_number"] or f"BDU-{order_id:05d}", category, severity, description,
               waiter["id"] if waiter else None, manager["id"] if manager else None, reference, identity_mode),
        )
        complaint = connection.execute(
            """SELECT complaints.*, orders.order_number, orders.created_at AS order_created_at,
               orders.items, waiter.firstname AS waiter_firstname, waiter.lastname AS waiter_lastname
               FROM complaints LEFT JOIN orders ON orders.id = complaints.order_id
               LEFT JOIN users AS waiter ON waiter.id = complaints.waiter_id
               WHERE complaints.id = ?""", (cursor.lastrowid,)
        ).fetchone()

    return jsonify(complaint_to_dict(complaint)), 201


@app.patch("/api/complaints/<int:complaint_id>")
@require_role("manager")
def update_complaint(complaint_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid complaint update is required"}), 400
    status = payload.get("status")

    if status not in VALID_STATUSES:
        return jsonify({"error": "Invalid complaint status"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        existing = connection.execute("SELECT id FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
        if not existing:
            return jsonify({"error": "Complaint not found"}), 404
        connection.execute(
            "UPDATE complaints SET status = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
            (status, timestamp, timestamp if status == "Resolved" else None, complaint_id),
        )
        complaint = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()

    return jsonify(complaint_to_dict(complaint))


@app.post("/api/complaints/<int:complaint_id>/response")
@require_role("manager")
def respond_to_complaint(complaint_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid response is required"}), 400
    response_text = str(payload.get("response", "")).strip()
    if not 1 <= len(response_text) <= 2000:
        return jsonify({"error": "A response between 1 and 2000 characters is required"}), 400
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        complaint = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()
        if not complaint:
            return jsonify({"error": "Complaint not found"}), 404
        if complaint["email_status"] == "sent" or complaint["sent_at"]:
            return jsonify({
                "error": "This response has already been sent and cannot be changed."
            }), 409
        if complaint["email_started_at"]:
            return jsonify({
                "error": "An email is currently being sent for this complaint."
            }), 409
        connection.execute(
            """
            UPDATE complaints
            SET manager_response = ?, responded_at = ?, email_status = 'pending',
                email_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (response_text, timestamp, timestamp, complaint_id),
        )
        updated = connection.execute("SELECT * FROM complaints WHERE id = ?", (complaint_id,)).fetchone()
    app.logger.info(
        "Manager user %s saved a response for complaint %s",
        g.current_user["id"],
        complaint_id,
    )
    return jsonify(complaint_to_dict(updated))


def send_email_message(message):
    host = os.environ.get("SMTP_HOST", "").strip()
    username = os.environ.get("SMTP_USER", "").strip()
    password = os.environ.get("SMTP_PASSWORD", "")
    if not all((host, username, password)):
        raise ValueError(
            "Email delivery is not configured. Set SMTP_HOST, SMTP_USER, "
            "and SMTP_PASSWORD in the workspace root .env file."
        )
    use_ssl = os.environ.get("SMTP_USE_SSL", "").strip().lower() in {
        "1", "true", "yes", "on"
    }
    try:
        port = int(os.environ.get("SMTP_PORT", "465" if use_ssl else "587"))
    except ValueError as error:
        raise ValueError("SMTP_PORT must be a valid port number.") from error
    if not 1 <= port <= 65535:
        raise ValueError("SMTP_PORT must be between 1 and 65535.")

    if not message.get("From"):
        message["From"] = os.environ.get("SMTP_FROM", "").strip() or username
    if use_ssl:
        with smtplib.SMTP_SSL(
            host, port, timeout=SMTP_TIMEOUT_SECONDS, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(username, password)
            smtp.send_message(message)
    else:
        with smtplib.SMTP(host, port, timeout=SMTP_TIMEOUT_SECONDS) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(username, password)
            smtp.send_message(message)


def send_complaint_email(complaint):
    username = os.environ.get("SMTP_USER", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip() or username
    reference = complaint["reference"] or f"CMP-{complaint['id']}"
    customer_name = complaint["customer_name"].strip() or "DineDesk customer"
    response_text = complaint["manager_response"]
    message = EmailMessage()
    message["Subject"] = f"Update regarding your DineDesk complaint {reference}"
    message["From"] = sender
    message["To"] = complaint["customer_email"]
    message.set_content(
        f"Hello {customer_name},\n\n"
        f"Thank you for contacting DineDesk about complaint {reference}.\n\n"
        f"Response from our manager:\n{response_text}\n\n"
        "Kind regards,\nDineDesk Customer Care"
    )
    message.add_alternative(
        f"""\
        <html>
          <body style="margin:0;background:#f8f4ed;font-family:Arial,sans-serif;color:#0a0f2e">
            <div style="max-width:600px;margin:32px auto;background:#ffffff;border-top:5px solid #c9a84c;padding:28px">
              <h1 style="margin:0 0 20px;color:#0a0f2e">DineDesk</h1>
              <p>Hello {escape(customer_name)},</p>
              <p>Thank you for contacting DineDesk about complaint <strong>{escape(reference)}</strong>.</p>
              <h2 style="font-size:18px">Response from our manager</h2>
              <div style="padding:16px;background:#f8f4ed;border-radius:6px;white-space:pre-wrap">{escape(response_text)}</div>
              <p style="margin-top:24px">Kind regards,<br><strong>DineDesk Customer Care</strong></p>
            </div>
          </body>
        </html>
        """,
        subtype="html",
    )

    send_email_message(message)


@app.post("/api/complaints/<int:complaint_id>/send-response")
@limiter.limit("10 per minute")
@require_role("manager")
def send_complaint_response(complaint_id):
    timestamp = datetime.now(timezone.utc).isoformat()
    stale_before = (
        datetime.now(timezone.utc) - timedelta(seconds=EMAIL_SEND_STALE_SECONDS)
    ).isoformat()
    with get_connection() as connection:
        complaint = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()
        if not complaint:
            return jsonify({"error": "Complaint not found"}), 404
        if not (complaint["manager_response"] or "").strip():
            return jsonify({"error": "Save a response before sending it."}), 400
        if complaint["email_status"] == "sent" or complaint["sent_at"]:
            return jsonify({
                "error": "This response has already been sent. Resending is disabled."
            }), 409

        customer_email = (complaint["customer_email"] or "").strip()
        if len(customer_email) > 254 or not EMAIL_PATTERN.fullmatch(customer_email):
            error_message = (
                "This complaint does not have a valid customer email address. "
                "The customer must provide an email before a response can be sent."
            )
            connection.execute(
                """
                UPDATE complaints
                SET email_status = 'failed', email_error = ?, email_started_at = NULL,
                    updated_at = ?
                WHERE id = ?
                """,
                (error_message, timestamp, complaint_id),
            )
            updated = connection.execute(
                "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
            ).fetchone()
            return jsonify({**complaint_to_dict(updated), "error": error_message}), 400

        reserved = connection.execute(
            """
            UPDATE complaints
            SET email_status = 'pending', email_error = NULL,
                email_started_at = ?, updated_at = ?
            WHERE id = ? AND sent_at IS NULL
              AND (email_started_at IS NULL OR email_started_at < ?)
            """,
            (timestamp, timestamp, complaint_id, stale_before),
        )
        if reserved.rowcount != 1:
            return jsonify({
                "error": "A response email is already being sent. Please wait and refresh."
            }), 409
        complaint = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()

    try:
        send_complaint_email(complaint)
    except (smtplib.SMTPException, OSError, ValueError) as error:
        if isinstance(error, smtplib.SMTPAuthenticationError):
            error_message = (
                "Email delivery failed because SMTP authentication was rejected. "
                "Check SMTP_USER and SMTP_PASSWORD."
            )
        elif isinstance(error, ValueError):
            error_message = str(error)
        else:
            error_message = (
                "Email delivery failed. Check the SMTP settings and connection, then retry."
            )
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE complaints
                SET email_status = 'failed', email_error = ?, email_started_at = NULL,
                    updated_at = ?
                WHERE id = ? AND email_started_at = ?
                """,
                (error_message, datetime.now(timezone.utc).isoformat(), complaint_id, timestamp),
            )
            updated = connection.execute(
                "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
            ).fetchone()
        app.logger.warning(
            "Complaint response email failed for complaint %s: %s",
            complaint_id,
            error,
        )
        status_code = 503 if isinstance(error, ValueError) else 502
        return jsonify({**complaint_to_dict(updated), "error": error_message}), status_code

    sent_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute(
            """
            UPDATE complaints
            SET email_status = 'sent', sent_at = ?, sent_by = ?, email_error = NULL,
                email_started_at = NULL, updated_at = ?
            WHERE id = ? AND email_started_at = ?
            """,
            (sent_at, g.current_user["id"], sent_at, complaint_id, timestamp),
        )
        updated = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()
    app.logger.info(
        "Manager user %s sent a response email for complaint %s",
        g.current_user["id"],
        complaint_id,
    )
    return jsonify(complaint_to_dict(updated))


@app.get("/api/health")
def health_check():
    return jsonify({"status": "ok"})

if os.environ.get("BUNDU_SKIP_DATABASE_INIT") != "1":
    initialise_database()

if __name__ == "__main__":
    app.run(debug=False, host="127.0.0.1", port=5000)