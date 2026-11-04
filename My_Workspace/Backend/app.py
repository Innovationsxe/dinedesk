from datetime import date, datetime, time, timedelta, timezone
from collections import Counter
import csv
from email.message import EmailMessage
from html import escape
import hashlib
import hmac
import math
import statistics
from functools import wraps
import json
import os
import pickle
from pathlib import Path
import re
import secrets
import smtplib
import ssl
import textwrap
import uuid
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import Request, urlopen
import jwt
from flask import Flask, Response, g, jsonify, render_template, request, send_from_directory
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

if __package__:
    from .complaint_ml import (
        CATEGORIES as COMPLAINT_CATEGORIES,
        draft_with_gemini,
        redact_personal_details,
        retrieve_solutions,
    )
    from .ml_training import (
        MODEL_TRAINED_AT_KEY,
        MODEL_VERSION_KEY,
        TEST_SPLIT_KEY,
        SKLEARN_AVAILABLE,
        MINIMUM_TRAINING_EXAMPLES,
        normalize_category,
        parse_category_label,
        predict_with_model,
        train_model,
        weekly_spike_report,
        wilson_interval,
    )
    from .database import (
        configure_database,
        get_connection as open_database_connection,
        get_database_errors,
        get_integrity_errors,
        get_table_columns,
    )
else:
    from complaint_ml import (
        CATEGORIES as COMPLAINT_CATEGORIES,
        draft_with_gemini,
        redact_personal_details,
        retrieve_solutions,
    )
    from ml_training import (
        MODEL_TRAINED_AT_KEY,
        MODEL_VERSION_KEY,
        TEST_SPLIT_KEY,
        SKLEARN_AVAILABLE,
        MINIMUM_TRAINING_EXAMPLES,
        normalize_category,
        parse_category_label,
        predict_with_model,
        train_model,
        weekly_spike_report,
        wilson_interval,
    )
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
UPLOAD_FOLDER = Path(os.environ.get("BUNDU_UPLOAD_FOLDER", BASE_DIR / "uploads"))
ML_MODEL_FOLDER = Path(
    os.environ.get("BUNDU_ML_MODEL_FOLDER", BASE_DIR / "ml_models")
)
RENDER_EXTERNAL_URL = os.environ.get("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
PUBLIC_APP_URL = os.environ.get(
    "BUNDU_PUBLIC_URL",
    "http://127.0.0.1:5000" if IS_DEVELOPMENT else RENDER_EXTERNAL_URL,
).strip().rstrip("/")
if PUBLIC_APP_URL:
    parsed_public_url = urlsplit(PUBLIC_APP_URL)
    if (
        parsed_public_url.scheme not in {"http", "https"}
        or not parsed_public_url.netloc
        or parsed_public_url.query
        or parsed_public_url.fragment
        or parsed_public_url.username
        or parsed_public_url.password
        or (
            parsed_public_url.scheme == "http"
            and (
                not IS_DEVELOPMENT
                or parsed_public_url.hostname not in {"localhost", "127.0.0.1", "::1"}
            )
        )
    ):
        raise RuntimeError(
            "BUNDU_PUBLIC_URL must be an HTTPS application URL "
            "(HTTP is permitted only for localhost in development)"
        )
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
JWT_SECRET = os.environ.get("BUNDU_JWT_SECRET", "")
if len(JWT_SECRET.encode("utf-8")) < 32:
    raise RuntimeError("BUNDU_JWT_SECRET must be set to a random value of at least 32 bytes")
MANAGER_REGISTRATION_KEY = os.environ.get("BUNDU_MANAGER_REGISTRATION_KEY", "")
if not IS_DEVELOPMENT and len(MANAGER_REGISTRATION_KEY.encode("utf-8")) < 32:
    raise RuntimeError(
        "BUNDU_MANAGER_REGISTRATION_KEY must be set to a random value of at least "
        "32 bytes for initial manager bootstrap"
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
        (
            "http://127.0.0.1:5500,http://localhost:5500"
            if IS_DEVELOPMENT
            else RENDER_EXTERNAL_URL
        ),
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
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "").strip()
PAYSTACK_CURRENCY = os.environ.get("PAYSTACK_CURRENCY", "ZAR").strip().upper()
if PAYSTACK_CURRENCY != "ZAR":
    raise RuntimeError("Paystack checkout currently supports PAYSTACK_CURRENCY=ZAR only")
FRONTEND_URL = os.environ.get("BUNDU_FRONTEND_URL", CORS_ORIGINS[0]).strip().rstrip("/")
parsed_frontend_url = urlsplit(FRONTEND_URL)
if (
    FRONTEND_URL not in CORS_ORIGINS
    or parsed_frontend_url.scheme not in {"http", "https"}
    or not parsed_frontend_url.netloc
    or parsed_frontend_url.path
    or parsed_frontend_url.query
    or parsed_frontend_url.fragment
    or parsed_frontend_url.username
    or parsed_frontend_url.password
):
    raise RuntimeError(
        "BUNDU_FRONTEND_URL must be an exact origin listed in BUNDU_ALLOWED_ORIGINS"
    )
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
VALID_CATEGORIES = set(COMPLAINT_CATEGORIES) | {
    "Wrong order",
    "Missing item",
    "Food temperature",
    "Long waiting time",
    "Poor service",
    "Incorrect bill",
    "Payment problem",
    "Hygiene concern",
}
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


def initialise_database(seed_menu=False):
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
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaint_training_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL,
                text_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                annotator_one_category TEXT,
                annotator_two_category TEXT
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaint_ml_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                complaint_id INTEGER NOT NULL UNIQUE,
                text TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'local_correction',
                created_at TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaint_ml_versions (
                version TEXT PRIMARY KEY,
                artifact_path TEXT NOT NULL,
                metrics TEXT NOT NULL,
                fixed_test_keys TEXT NOT NULL,
                created_at TEXT NOT NULL,
                deployed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS complaint_ml_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO complaint_ml_settings
                (setting_key, setting_value)
            VALUES ('enabled', '1')
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
            CREATE TABLE IF NOT EXISTS staff_invitations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('waiter', 'manager')),
                key_hash TEXT NOT NULL UNIQUE,
                generated_by INTEGER NOT NULL,
                generated_by_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used_at TEXT,
                status TEXT NOT NULL DEFAULT 'current'
                    CHECK (status IN ('current', 'used', 'replaced'))
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
                    CHECK (payment_method IN ('cash', 'card')),
                payment_provider TEXT NOT NULL DEFAULT 'in_person',
                payment_status TEXT NOT NULL DEFAULT 'unpaid',
                payment_reference TEXT
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
            CREATE TABLE IF NOT EXISTS daily_revenue_resets (
                business_date TEXT PRIMARY KEY,
                reset_at TEXT NOT NULL,
                reset_by INTEGER NOT NULL
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
        complaint_viewed_column_missing = (
            "manager_viewed_at" not in get_table_columns(connection, "complaints")
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
            ("orders", "payment_provider TEXT NOT NULL DEFAULT 'in_person'"),
            ("orders", "payment_status TEXT NOT NULL DEFAULT 'unpaid'"),
            ("orders", "payment_reference TEXT"),
            ("orders", "rating INTEGER"),
            ("orders", "served_at TEXT"),
            ("orders", "cancellation_reason TEXT"),
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
            ("complaints", "customer_rating INTEGER"),
            ("complaints", "customer_rating_at TEXT"),
            ("complaints", "feedback_token_hash TEXT"),
            ("complaints", "ml_suggested_category TEXT"),
            ("complaints", "ml_confidence REAL"),
            ("complaints", "ml_model_version TEXT"),
            ("complaints", "ml_corrected_category TEXT"),
            ("complaints", "ml_category_corrected_at TEXT"),
            ("complaints", "ml_first_suggestion_accepted INTEGER"),
            ("complaints", "ml_first_suggestion_at TEXT"),
            ("complaints", "ml_suggested_response TEXT"),
            ("complaints", "ml_first_response_accepted INTEGER"),
            ("complaints", "ml_first_response_at TEXT"),
            ("complaints", "manager_viewed_at TEXT"),
        ]:
            column_name = column.split(maxsplit=1)[0]
            existing_columns = get_table_columns(connection, table)
            if column_name not in existing_columns:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {column}")
        if complaint_viewed_column_missing:
            connection.execute(
                "UPDATE complaints SET manager_viewed_at = COALESCE(updated_at, created_at)"
            )
        training_columns = get_table_columns(
            connection,
            "complaint_training_examples",
        )
        for column in (
            "annotator_one_category TEXT",
            "annotator_two_category TEXT",
        ):
            if column.split(maxsplit=1)[0] not in training_columns:
                connection.execute(
                    f"ALTER TABLE complaint_training_examples ADD COLUMN {column}"
                )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_complaint_feedback_token "
            "ON complaints(feedback_token_hash)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_orders_payment_reference "
            "ON orders(payment_reference)"
        )
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
        connection.execute(
            "UPDATE menu_items SET category = 'Mains' "
            "WHERE LOWER(TRIM(category)) = 'main'"
        )


def complaint_to_dict(row):
    complaint = dict(row)
    complaint.pop("feedback_token_hash", None)
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
        "served_at": order.get("served_at"),
        "cancellation_reason": order.get("cancellation_reason"),
        "order_type": order["order_type"],
        "payment_method": (
            "online"
            if order.get("payment_provider") == "paystack"
            else order["payment_method"]
        ),
        "payment_status": order.get("payment_status", "unpaid"),
        "payment_provider": order.get("payment_provider", "in_person"),
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
    supplied_key = str(payload.get("registration_key", ""))
    if not 20 <= len(supplied_key) <= 128:
        return jsonify({"error": "A valid staff invitation key is required"}), 403
    invitation_hash = hashlib.sha256(supplied_key.encode("utf-8")).hexdigest()
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            invitation = connection.execute(
                """
                SELECT id, email, role, expires_at, status
                FROM staff_invitations WHERE key_hash = ?
                """,
                (invitation_hash,),
            ).fetchone()
            valid_invitation = bool(
                invitation
                and invitation["status"] == "current"
                and invitation["email"].casefold() == email.casefold()
                and invitation["role"] == role
                and invitation["expires_at"] > timestamp
            )
            initial_manager_key = (
                role == "manager"
                and bool(MANAGER_REGISTRATION_KEY)
                and hmac.compare_digest(supplied_key, MANAGER_REGISTRATION_KEY)
            )
            if not valid_invitation and initial_manager_key:
                active_manager = connection.execute(
                    "SELECT 1 FROM users WHERE role = 'manager' AND is_active = 1 LIMIT 1"
                ).fetchone()
                initial_manager_key = active_manager is None
            if not valid_invitation and not initial_manager_key:
                return jsonify({"error": "A valid invitation key for this email and account type is required"}), 403
            roster_member = connection.execute(
                "SELECT id, role FROM staff_roster WHERE email = ? COLLATE NOCASE",
                (email,),
            ).fetchone()
            if roster_member and roster_member["role"] != role:
                return jsonify({"error": "This invitation does not match the staff member's assigned role"}), 403
            if valid_invitation:
                updated_invitation = connection.execute(
                    """
                    UPDATE staff_invitations SET status = 'used', used_at = ?
                    WHERE id = ? AND status = 'current'
                    """,
                    (timestamp, invitation["id"]),
                )
                if updated_invitation.rowcount != 1:
                    return jsonify({"error": "This staff invitation has already been used"}), 403
            cursor = connection.execute(
                """
                INSERT INTO users (firstname, lastname, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (firstname, lastname, email, generate_password_hash(password, method="scrypt"), role, timestamp),
            )
            if roster_member:
                connection.execute(
                    """
                    UPDATE staff_roster
                    SET user_id = ?, firstname = ?, lastname = ?
                    WHERE id = ?
                    """,
                    (cursor.lastrowid, firstname, lastname, roster_member["id"]),
                )
            else:
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


@app.post("/api/staff/invitations")
@require_role("manager")
def create_staff_invitation():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid staff invitation is required"}), 400
    email = str(payload.get("email", "")).strip().lower()
    role = str(payload.get("role", "")).strip().lower()
    if len(email) > 254 or not EMAIL_PATTERN.fullmatch(email) or role not in VALID_ROLES:
        return jsonify({"error": "Enter a valid email address and staff account type"}), 400

    invitation_key = secrets.token_urlsafe(32)
    key_hash = hashlib.sha256(invitation_key.encode("utf-8")).hexdigest()
    timestamp = datetime.now(timezone.utc)
    expires_at = (timestamp + timedelta(days=7)).isoformat()
    created_at = timestamp.isoformat()
    generated_by_name = (
        f'{g.current_user["firstname"]} {g.current_user["lastname"]}'.strip()
    )
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active_user = connection.execute(
                "SELECT 1 FROM users WHERE email = ? COLLATE NOCASE AND is_active = 1",
                (email,),
            ).fetchone()
            if active_user:
                return jsonify({"error": "An active account already uses that email"}), 409
            roster_member = connection.execute(
                "SELECT role FROM staff_roster WHERE email = ? COLLATE NOCASE",
                (email,),
            ).fetchone()
            if roster_member and roster_member["role"] != role:
                return jsonify({"error": "The invitation role must match the staff member's assigned role"}), 400
            connection.execute(
                """
                UPDATE staff_invitations SET status = 'replaced'
                WHERE email = ? COLLATE NOCASE AND role = ? AND status = 'current'
                """,
                (email, role),
            )
            connection.execute(
                """
                INSERT INTO staff_invitations
                    (email, role, key_hash, generated_by, generated_by_name,
                     created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email, role, key_hash, g.current_user["id"],
                    generated_by_name, created_at, expires_at,
                ),
            )
    except DATABASE_INTEGRITY_ERRORS:
        return jsonify({"error": "Could not create an invitation for that email"}), 409

    return jsonify({
        "email": email,
        "role": role,
        "invitation_key": invitation_key,
        "expires_at": expires_at,
    }), 201


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
    if "password" in payload:
        return jsonify({"error": "Staff passwords are generated separately"}), 400
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
    if role in {"waiter", "manager"} and not email:
        return jsonify({
            "error": "Waiters and managers need a valid email address"
        }), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            user_id = None
            if role in {"waiter", "manager"}:
                stored_password = secrets.token_urlsafe(32)
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

    remember_device = payload.get("remember_device") is True
    token_lifetime = timedelta(days=30) if remember_device else timedelta(hours=12)
    token = jwt.encode(
        {
            "sub": email,
            "exp": (timestamp + token_lifetime).timestamp(),
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


class PaystackPaymentError(RuntimeError):
    pass


def paystack_is_configured():
    return bool(PAYSTACK_SECRET_KEY and FRONTEND_URL)


def paystack_api_request(path, payload=None):
    if not PAYSTACK_SECRET_KEY:
        raise PaystackPaymentError("Paystack is not configured")
    request_data = (
        json.dumps(payload).encode("utf-8") if payload is not None else None
    )
    paystack_request = Request(
        f"https://api.paystack.co{path}",
        data=request_data,
        headers={
            "Authorization": f"Bearer {PAYSTACK_SECRET_KEY}",
            "Content-Type": "application/json",
        },
        method="POST" if request_data is not None else "GET",
    )
    try:
        with urlopen(paystack_request, timeout=15) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        try:
            response_body = json.loads(error.read())
            provider_message = (
                response_body.get("message", "")
                if isinstance(response_body, dict)
                else ""
            )
        except (json.JSONDecodeError, AttributeError):
            provider_message = ""
        app.logger.error(
            "Paystack API request failed (HTTP %s): %s",
            error.code,
            provider_message or "no error details returned",
        )
        raise PaystackPaymentError("Paystack could not process the payment") from error
    except (URLError, TimeoutError, OSError) as error:
        app.logger.error("Paystack API request failed: %s", error)
        raise PaystackPaymentError(
            "Could not connect to Paystack to process the payment"
        ) from error
    except json.JSONDecodeError as error:
        app.logger.error("Paystack returned invalid JSON")
        raise PaystackPaymentError("Paystack returned an invalid response") from error
    if not isinstance(result, dict) or result.get("status") is not True:
        message = result.get("message", "") if isinstance(result, dict) else ""
        app.logger.error("Paystack returned an unsuccessful API response: %s", message)
        raise PaystackPaymentError("Paystack could not process the payment")
    data = result.get("data")
    if not isinstance(data, dict):
        app.logger.error("Paystack returned an invalid API response")
        raise PaystackPaymentError("Paystack returned an invalid response")
    return data


def initialize_paystack_transaction(order, reference):
    if not paystack_is_configured():
        raise PaystackPaymentError("Paystack is not configured")
    callback_query = urlencode(
        {
            "order": order["id"],
            "checkout": "paystack",
            "reference": reference,
        }
    )
    callback_url = f"{FRONTEND_URL}/?{callback_query}"
    transaction = paystack_api_request(
        "/transaction/initialize",
        {
            "email": order["customer_email"],
            "amount": int(round(float(order["total"]) * 100)),
            "currency": PAYSTACK_CURRENCY,
            "reference": reference,
            "callback_url": callback_url,
            "metadata": {
                "order_id": str(order["id"]),
                "order_number": order.get("order_number", ""),
            },
        },
    )
    authorization_url = transaction.get("authorization_url")
    returned_reference = transaction.get("reference")
    if (
        not isinstance(authorization_url, str)
        or not isinstance(returned_reference, str)
        or returned_reference != reference
    ):
        app.logger.error("Paystack returned invalid initialization data for order %s", order["id"])
        raise PaystackPaymentError("Paystack returned an invalid checkout response")
    parsed_url = urlsplit(authorization_url)
    if (
        parsed_url.scheme != "https"
        or parsed_url.hostname != "checkout.paystack.com"
    ):
        app.logger.error("Paystack returned an invalid checkout URL for order %s", order["id"])
        raise PaystackPaymentError("Paystack returned an invalid checkout URL")
    return {"reference": reference, "url": authorization_url}


def paystack_signature_is_valid(payload, signature_header):
    if not PAYSTACK_SECRET_KEY or len(signature_header) != 128:
        return False
    if any(character not in "0123456789abcdefABCDEF" for character in signature_header):
        return False
    expected_signature = hmac.new(
        PAYSTACK_SECRET_KEY.encode("utf-8"), payload, hashlib.sha512
    ).hexdigest()
    return hmac.compare_digest(expected_signature, signature_header.lower())


def confirm_paystack_transaction(reference, transaction):
    if (
        not isinstance(transaction, dict)
        or transaction.get("status") != "success"
        or transaction.get("reference") != reference
    ):
        return False
    amount = transaction.get("amount")
    currency = transaction.get("currency")
    if (
        isinstance(amount, bool)
        or not isinstance(amount, int)
        or not isinstance(currency, str)
    ):
        raise PaystackPaymentError("Paystack transaction amount is invalid")

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            """
            SELECT id, total, status, payment_status
            FROM orders
            WHERE payment_reference = ? AND payment_provider = 'paystack'
            """,
            (reference,),
        ).fetchone()
        if not order:
            return False
        expected_amount = int(round(float(order["total"]) * 100))
        if amount != expected_amount or currency.upper() != PAYSTACK_CURRENCY:
            app.logger.error("Paystack payment amount mismatch for order %s", order["id"])
            raise PaystackPaymentError("Paystack transaction does not match the order")
        if order["payment_status"] != "paid":
            connection.execute(
                """
                UPDATE orders SET payment_status = 'paid',
                    status = CASE
                        WHEN status IN ('Awaiting payment', 'Cancelled') THEN 'Received'
                        ELSE status
                    END,
                    updated_at = ?
                WHERE id = ? AND payment_reference = ?
                  AND payment_provider = 'paystack' AND payment_status != 'paid'
                """,
                (timestamp, order["id"], reference),
            )
    return True


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
    order_type = str(payload.get("order_type", "Dine in")).strip()
    payment_method_value = payload.get("payment_method", "cash")
    if not isinstance(payment_method_value, str):
        return jsonify({"error": "Choose cash or card as the payment method"}), 400
    payment_method = payment_method_value.strip().lower()
    if payment_method not in {"cash", "card", "online"}:
        return jsonify({"error": "Choose cash, card, or online as the payment method"}), 400
    if payment_method == "online" and not customer_email:
        return jsonify({
            "error": "Enter an email address for online payment, or choose cash or card."
        }), 400
    if payment_method == "online" and not paystack_is_configured():
        return jsonify({
            "error": "Online payment is not configured. Please choose cash or card."
        }), 503
    table_number = payload.get("table_number")
    items = payload.get("items")

    if len(customer_name) > 160:
        return jsonify({"error": "Customer names must be 160 characters or fewer"}), 400
    if customer_email and (len(customer_email) > 254 or not EMAIL_PATTERN.fullmatch(customer_email)):
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
    if payment_method == "online" and total <= 0:
        return jsonify({"error": "Online payment requires an order total greater than zero"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    tracking_token = secrets.token_urlsafe(32)
    stored_payment_method = "card" if payment_method == "online" else payment_method
    payment_provider = "paystack" if payment_method == "online" else "in_person"
    payment_status = "pending" if payment_method == "online" else "unpaid"
    initial_status = "Awaiting payment" if payment_method == "online" else "Received"
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
                 payment_provider, payment_status, table_number, tracking_token_hash,
                 created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                customer_name,
                customer_email or "Not provided",
                json.dumps(clean_items),
                total,
                initial_status,
                order_type,
                stored_payment_method,
                payment_provider,
                payment_status,
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
    if payment_method == "online":
        payment_reference = f"BDU-{order['id']}-{secrets.token_hex(12)}"
        with get_connection() as connection:
            connection.execute(
                """
                UPDATE orders SET payment_reference = ?
                WHERE id = ? AND payment_provider = 'paystack'
                  AND payment_status = 'pending'
                """,
                (payment_reference, order["id"]),
            )
            order = connection.execute(
                "SELECT * FROM orders WHERE id = ?", (order["id"],)
            ).fetchone()
        try:
            checkout = initialize_paystack_transaction(order, payment_reference)
        except PaystackPaymentError as error:
            app.logger.error("Paystack checkout could not be created for order %s: %s", order["id"], error)
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE orders SET status = 'Cancelled', payment_status = 'failed',
                        updated_at = ?
                    WHERE id = ? AND payment_provider = 'paystack'
                      AND payment_status = 'pending'
                    """,
                    (datetime.now(timezone.utc).isoformat(), order["id"]),
                )
            return jsonify({
                "error": "Could not start online payment. Please try again or choose another payment method."
            }), 503
        result["payment_provider"] = "paystack"
        result["checkout_url"] = checkout["url"]
        result["payment_reference"] = checkout["reference"]
    return jsonify(result), 201


@app.get("/api/orders")
@require_role("waiter", "manager")
def list_orders():
    with get_connection() as connection:
        active = request.args.get('active', '').lower() == 'true'
        where = (
            "WHERE NOT (orders.payment_provider = 'paystack' "
            "AND orders.payment_status != 'paid')"
        )
        if active:
            where += " AND orders.status NOT IN ('Completed', 'Paid', 'Cancelled')"
        orders = connection.execute(
            f"""
            SELECT orders.*, users.firstname || ' ' || users.lastname AS waiter_name
            FROM orders LEFT JOIN users ON users.id = orders.waiter_id
            {where}
            ORDER BY orders.created_at DESC
            """
        ).fetchall()
    return jsonify([public_order(order) for order in orders])


@app.get("/api/manager/revenue/resets")
@require_role("manager")
def list_daily_revenue_resets():
    with get_connection() as connection:
        resets = connection.execute(
            "SELECT business_date, reset_at FROM daily_revenue_resets "
            "ORDER BY business_date DESC"
        ).fetchall()
    return jsonify([dict(reset) for reset in resets])


@app.post("/api/manager/revenue/reset")
@require_role("manager")
def reset_daily_revenue():
    business_date = datetime.now(SOUTH_AFRICA_TIMEZONE).date().isoformat()
    reset_at = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        connection.execute(
            """
            INSERT INTO daily_revenue_resets (business_date, reset_at, reset_by)
            VALUES (?, ?, ?)
            ON CONFLICT(business_date) DO UPDATE SET
                reset_at = excluded.reset_at,
                reset_by = excluded.reset_by
            """,
            (business_date, reset_at, g.current_user["id"]),
        )
    return jsonify({"business_date": business_date, "reset_at": reset_at})


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
    if status == "Cancelled":
        return jsonify({"error": "Use the overdue-order cancellation action to cancel an order"}), 409

    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        current_order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not current_order:
            return jsonify({"error": "Order not found"}), 404
        if current_order["status"] == "Cancelled":
            return jsonify({"error": "Cancelled orders cannot be changed"}), 409
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
            or (
                current_order["payment_provider"] == "paystack"
                and current_order["payment_status"] != "paid"
            )
        ):
            return jsonify({"error": "Payment can be recorded after a dine-in order is served or a takeaway order is ready, and online orders must be confirmed by Paystack"}), 409
        cursor = connection.execute(
            """
            UPDATE orders SET status = ?, updated_at = ?,
                waiter_id = COALESCE(waiter_id, ?),
                served_at = CASE
                    WHEN ? = 'Served' AND status = 'Served' THEN served_at
                    WHEN ? = 'Served' THEN ?
                    ELSE NULL
                END,
                payment_status = CASE
                    WHEN ? = 'Paid' AND payment_provider != 'paystack' THEN 'paid'
                    ELSE payment_status
                END
            WHERE id = ?
            """,
            (
                status,
                timestamp,
                g.current_user["id"],
                status,
                status,
                timestamp,
                status,
                order_id,
            ),
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


@app.patch("/api/orders/<int:order_id>/cancel")
@require_role("waiter", "manager")
def cancel_overdue_order(order_id):
    timestamp = datetime.now(timezone.utc)
    cutoff = timestamp - timedelta(hours=2)
    with get_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
        if not order:
            return jsonify({"error": "Order not found"}), 404
        if (
            g.current_user["role"] == "waiter"
            and order["waiter_id"] != g.current_user["id"]
        ):
            return jsonify({"error": "Only the assigned waiter can cancel this order"}), 403
        if (
            order["status"] != "Served"
            or order["order_type"] != "Dine in"
            or order["payment_provider"] != "in_person"
            or order["payment_status"] != "unpaid"
        ):
            return jsonify(
                {"error": "Only unpaid, in-person dine-in orders can be cancelled"}
            ), 409
        try:
            served_at = datetime.fromisoformat(
                str(order["served_at"]).replace("Z", "+00:00")
            )
            if served_at.tzinfo is None:
                served_at = served_at.replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            return jsonify(
                {"error": "This order does not have a recorded served time"}
            ), 409
        if served_at > cutoff:
            return jsonify(
                {"error": "An order can only be cancelled after it has been served for two hours"}
            ), 409
        cursor = connection.execute(
            """
            UPDATE orders
            SET status = 'Cancelled', cancellation_reason = ?,
                updated_at = ?, served_at = NULL
            WHERE id = ? AND status = 'Served' AND order_type = 'Dine in'
              AND payment_provider = 'in_person' AND payment_status = 'unpaid'
              AND served_at <= ?
              AND (? = 'manager' OR waiter_id = ?)
            """,
            (
                "Served for over two hours without collection",
                timestamp.isoformat(),
                order_id,
                cutoff.isoformat(),
                g.current_user["role"],
                g.current_user["id"],
            ),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "The order is no longer eligible for cancellation"}), 409
        cancelled_order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    return jsonify(public_order(cancelled_order))


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
            or order["payment_provider"] == "paystack"
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


@app.post("/api/orders/<int:order_id>/paystack/verify")
@limiter.limit("10 per minute")
def verify_paystack_order(order_id):
    if not paystack_is_configured():
        return jsonify({"error": "Paystack is not configured"}), 503
    tracking_token = request.headers.get("X-Order-Tracking-Token", "")
    with get_connection() as connection:
        order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    if not order or not order_tracking_is_valid(order, tracking_token):
        return jsonify({"error": "Order not found"}), 404
    if order["payment_provider"] != "paystack" or not order["payment_reference"]:
        return jsonify({"error": "This order does not have a pending Paystack payment"}), 409
    if order["payment_status"] == "paid":
        return jsonify(public_order(order))

    try:
        transaction = paystack_api_request(
            f"/transaction/verify/{quote(order['payment_reference'], safe='')}"
        )
        confirm_paystack_transaction(order["payment_reference"], transaction)
    except PaystackPaymentError as error:
        app.logger.error("Could not verify Paystack payment for order %s: %s", order_id, error)
        return jsonify({"error": "Could not verify the payment with Paystack"}), 503

    with get_connection() as connection:
        updated_order = connection.execute(
            "SELECT * FROM orders WHERE id = ?", (order_id,)
        ).fetchone()
    return jsonify(public_order(updated_order))


@app.post("/api/payments/paystack/webhook")
def paystack_webhook():
    if not PAYSTACK_SECRET_KEY:
        return jsonify({"error": "Paystack webhooks are not configured"}), 503

    payload = request.get_data(cache=False)
    if not paystack_signature_is_valid(
        payload,
        request.headers.get("x-paystack-signature", ""),
    ):
        app.logger.warning("Rejected Paystack webhook with an invalid signature")
        return jsonify({"error": "Invalid Paystack webhook signature"}), 400
    try:
        event = json.loads(payload)
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid Paystack webhook payload"}), 400
    if not isinstance(event, dict):
        return jsonify({"error": "Invalid Paystack webhook payload"}), 400
    if event.get("event") != "charge.success":
        return jsonify({"received": True})

    event_data = event.get("data")
    if not isinstance(event_data, dict):
        return jsonify({"error": "Invalid Paystack event"}), 400
    reference = event_data.get("reference")
    if not isinstance(reference, str) or not reference:
        return jsonify({"error": "Invalid Paystack event"}), 400
    try:
        confirm_paystack_transaction(reference, event_data)
    except PaystackPaymentError:
        return jsonify({"error": "Paystack transaction does not match the order"}), 400
    return jsonify({"received": True})


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


@app.get("/feedback/<token>")
def complaint_feedback_page(token):
    response = Response(
        render_template("feedback.html", feedback_token=token),
        mimetype="text/html",
    )
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.post("/api/complaints/feedback")
@limiter.limit("10 per minute")
def rate_complaint_solution():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid feedback response is required"}), 400
    rating = payload.get("rating")
    token = payload.get("token")
    if isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5:
        return jsonify({"error": "Choose a rating from 1 to 5"}), 400
    if not isinstance(token, str) or not 20 <= len(token) <= 128:
        return jsonify({"error": "This feedback link is invalid or expired"}), 404

    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            complaint = connection.execute(
                """
                SELECT id, sent_at, customer_rating
                FROM complaints
                WHERE feedback_token_hash = ?
                """,
                (token_hash,),
            ).fetchone()
            if not complaint or not complaint["sent_at"]:
                return jsonify({"error": "This feedback link is invalid or expired"}), 404
            if complaint["customer_rating"] is not None:
                return jsonify({"error": "This solution has already been rated"}), 409
            updated = connection.execute(
                """
                UPDATE complaints
                SET customer_rating = ?, customer_rating_at = ?, updated_at = ?
                WHERE id = ? AND feedback_token_hash = ? AND customer_rating IS NULL
                """,
                (rating, timestamp, timestamp, complaint["id"], token_hash),
            )
            if updated.rowcount != 1:
                return jsonify({"error": "This solution has already been rated"}), 409
    except DATABASE_ERRORS:
        app.logger.exception("Could not save complaint solution feedback")
        return jsonify({"error": "Could not save your rating"}), 500
    return jsonify({"message": "Thank you for rating the manager's solution."})


@app.get("/api/pantry")
@require_role("manager")
def list_pantry_items():
    with get_connection() as connection:
        rows = connection.execute("SELECT * FROM pantry_items ORDER BY name COLLATE NOCASE").fetchall()
        today = datetime.now(SOUTH_AFRICA_TIMEZONE).date()
        week_start = today - timedelta(days=6)
        usage_rows = connection.execute(
            """
            SELECT item_id,
                   SUM(CASE WHEN movement_date = ? THEN -quantity_change ELSE 0 END) AS used_today,
                   SUM(-quantity_change) AS used_last_seven_days
            FROM stock_movements
            WHERE reason = 'Usage' AND movement_date >= ? AND movement_date <= ?
            GROUP BY item_id
            """,
            (today.isoformat(), week_start.isoformat(), today.isoformat()),
        ).fetchall()
    usage_by_item = {row["item_id"]: row for row in usage_rows}
    items = [pantry_item_to_dict(row) for row in rows]
    for item in items:
        usage = usage_by_item.get(item["id"])
        used_today = float(usage["used_today"] or 0) if usage else 0
        used_last_seven_days = (
            float(usage["used_last_seven_days"] or 0) if usage else 0
        )
        average_daily_usage = used_last_seven_days / 7
        item["used_today"] = used_today
        item["average_daily_usage"] = average_daily_usage
        item["days_until_reorder"] = (
            max(0, item["quantity"] - item["reorder_level"]) / average_daily_usage
            if average_daily_usage > 0
            else None
        )
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
        if datetime.strptime(movement_date, "%Y-%m-%d").date() > datetime.now(
            SOUTH_AFRICA_TIMEZONE
        ).date():
            raise ValueError("Movement date cannot be in the future")
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


@app.get("/api/manager/complaints/unread-count")
@require_role("manager")
def count_unread_complaints():
    with get_connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM complaints WHERE manager_viewed_at IS NULL"
        ).fetchone()[0]
    return jsonify({"count": count})


@app.post("/api/complaints/<int:complaint_id>/open")
@require_role("manager")
def open_complaint(complaint_id):
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_connection() as connection:
        cursor = connection.execute(
            """
            UPDATE complaints
            SET manager_viewed_at = COALESCE(manager_viewed_at, ?)
            WHERE id = ?
            """,
            (timestamp, complaint_id),
        )
        if cursor.rowcount == 0:
            return jsonify({"error": "Complaint not found"}), 404
        complaint = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()
    return jsonify(complaint_to_dict(complaint))


def complaint_ml_is_enabled(connection):
    setting = connection.execute(
        "SELECT setting_value FROM complaint_ml_settings WHERE setting_key = 'enabled'"
    ).fetchone()
    return bool(setting and setting["setting_value"] == "1")


def load_complaint_ml_model(connection):
    current = connection.execute(
        """
        SELECT versions.version, versions.artifact_path, versions.metrics,
               versions.fixed_test_keys, versions.created_at
        FROM complaint_ml_settings AS settings
        JOIN complaint_ml_versions AS versions
          ON versions.version = settings.setting_value
        WHERE settings.setting_key = ?
          AND versions.deployed = 1
        """,
        (MODEL_VERSION_KEY,),
    ).fetchone()
    if not current:
        return None, None
    if not SKLEARN_AVAILABLE:
        return None, dict(current)
    artifact_path = (BASE_DIR / current["artifact_path"]).resolve()
    try:
        artifact_path.relative_to(ML_MODEL_FOLDER.resolve())
    except ValueError as error:
        raise RuntimeError("The stored model path is outside the model directory.") from error
    with artifact_path.open("rb") as model_file:
        model = pickle.load(model_file)
    return model, dict(current)


def complaint_ml_training_records(connection):
    examples = connection.execute(
        """
        SELECT id, text, category, source, created_at
        FROM complaint_training_examples
        """
    ).fetchall()
    corrections = connection.execute(
        """
        SELECT id, text, category, source, created_at
        FROM complaint_ml_labels
        """
    ).fetchall()
    records = [
        {
            "key": f"example:{row['id']}",
            "text": row["text"],
            "category": normalize_category(row["category"]),
            "source": row["source"],
            "created_at": row["created_at"],
        }
        for row in examples
    ]
    records.extend(
        {
            "key": f"label:{row['id']}",
            "text": row["text"],
            "category": normalize_category(row["category"]),
            "source": row["source"],
            "created_at": row["created_at"],
        }
        for row in corrections
    )
    return records


def complaint_ml_context(connection, exclude_complaint_id=None, predicted_category=None):
    complaint_rows = connection.execute(
        """
        SELECT id, customer_name, customer_email, description, message, category,
               ml_corrected_category, ml_confidence, manager_response, status,
               customer_rating
        FROM complaints
        """
    ).fetchall()
    known_details = []
    for row in complaint_rows:
        if row["customer_name"]:
            known_details.append(row["customer_name"])
        if row["customer_email"] and row["customer_email"] != "Not provided":
            known_details.append(row["customer_email"])

    examples = connection.execute(
        """
        SELECT text, category, source FROM complaint_training_examples
        """
    ).fetchall()
    labeled_examples = [
        {
            "text": redact_personal_details(example["text"], known_details),
            "category": normalize_category(example["category"]),
            "source": example["source"],
        }
        for example in examples
    ]
    solutions = []
    solution_ratings = {}
    for row in complaint_rows:
        response = (row["manager_response"] or "").strip()
        if row["status"] == "Resolved" and response and row["customer_rating"] is not None:
            rating_key = response.casefold()
            totals = solution_ratings.setdefault(rating_key, [0, 0])
            totals[0] += row["customer_rating"]
            totals[1] += 1
    for row in complaint_rows:
        text = row["description"] or row["message"] or ""
        category = row["ml_corrected_category"] or row["category"]
        if (
            row["id"] != exclude_complaint_id
            and category
            and text.strip()
        ):
            labeled_examples.append(
                {
                    "text": redact_personal_details(text, known_details),
                    "category": normalize_category(category),
                    "source": "local_complaint",
                }
            )
        if (
            row["id"] != exclude_complaint_id
            and row["status"] == "Resolved"
            and (row["manager_response"] or "").strip()
            and (
                predicted_category is None
                or normalize_category(category) == predicted_category
            )
        ):
            solutions.append(
                {
                    "id": row["id"],
                    "text": text,
                    "solution": row["manager_response"],
                    "category": normalize_category(category),
                    "rating": row["customer_rating"],
                    "rating_sum": solution_ratings.get(
                        row["manager_response"].strip().casefold(), [0, 0]
                    )[0],
                    "rating_count": solution_ratings.get(
                        row["manager_response"].strip().casefold(), [0, 0]
                    )[1],
                }
            )
    return labeled_examples, solutions, known_details


@app.get("/api/manager/complaints/insights")
@require_role("manager")
def complaint_insights():
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=90)
    week_start = now.date() - timedelta(days=now.weekday())
    window_start_date = window_start.date().isoformat()
    try:
        with get_connection() as connection:
            complaints = connection.execute(
                """
                SELECT complaints.order_id, complaints.category,
                       complaints.ml_corrected_category, complaints.status,
                       complaints.customer_rating, complaints.created_at,
                       complaints.resolved_at, complaints.ml_first_suggestion_accepted,
                       complaints.ml_first_suggestion_at,
                       complaints.ml_first_response_accepted,
                       complaints.ml_first_response_at,
                       complaints.customer_rating_at
                FROM complaints
                WHERE substr(complaints.created_at, 1, 10) >= ?
                """,
                (window_start_date,),
            ).fetchall()
            orders = connection.execute(
                """
                SELECT id, status, rating, created_at
                FROM orders
                WHERE substr(created_at, 1, 10) >= ?
                """,
                (window_start_date,),
            ).fetchall()
            labels = connection.execute(
                """
                SELECT annotator_one_category, annotator_two_category
                FROM complaint_training_examples
                WHERE annotator_one_category IS NOT NULL
                  AND annotator_two_category IS NOT NULL
                """
            ).fetchall()
            model, current_model = load_complaint_ml_model(connection)
            ml_enabled = complaint_ml_is_enabled(connection)
    except DATABASE_ERRORS:
        app.logger.exception("Could not load complaint service-quality insights")
        return jsonify({"error": "Could not load service-quality insights"}), 500
    except (ImportError, OSError, pickle.PickleError, RuntimeError):
        app.logger.exception("Could not load the active complaint classifier")
        return jsonify({"error": "Could not load the active complaint classifier"}), 500

    categorized = {category: [] for category in COMPLAINT_CATEGORIES}
    for complaint in complaints:
        category = normalize_category(
            complaint["ml_corrected_category"] or complaint["category"]
        )
        if category not in categorized:
            continue
        try:
            created_at = datetime.fromisoformat(
                str(complaint["created_at"]).replace("Z", "+00:00")
            )
        except (TypeError, ValueError):
            continue
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        categorized[category].append((complaint, created_at))

    category_summary = []
    for category, entries in categorized.items():
        resolved_count = sum(row["status"] == "Resolved" for row, _ in entries)
        ratings = [
            row["customer_rating"]
            for row, _ in entries
            if row["customer_rating"] is not None
        ]
        category_summary.append(
            {
                "category": category,
                "frequency_90_days": len(entries),
                "resolution_rate": round(resolved_count / len(entries), 4)
                if entries
                else None,
                "resolution_rate_ci": wilson_interval(resolved_count, len(entries)),
                "average_solution_rating": round(statistics.fmean(ratings), 2)
                if ratings
                else None,
                "solution_ratings": len(ratings),
            }
        )

    weekly_counts = []
    for offset in range(9):
        monday = week_start - timedelta(weeks=8 - offset)
        next_monday = monday + timedelta(days=7)
        week_counts = {category: 0 for category in COMPLAINT_CATEGORIES}
        for complaint in complaints:
            try:
                created_at = datetime.fromisoformat(
                    str(complaint["created_at"]).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                continue
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            local_date = created_at.date()
            category = normalize_category(
                complaint["ml_corrected_category"] or complaint["category"]
            )
            if monday <= local_date < next_monday and category in week_counts:
                week_counts[category] += 1
        weekly_counts.append(week_counts)
    spikes = weekly_spike_report(weekly_counts)

    all_complaint_ratings = [
        complaint["customer_rating"]
        for complaint in complaints
        if complaint["customer_rating"] is not None
    ]
    completed_orders = sum(
        order["status"] in {"Completed", "Paid"} for order in orders
    )
    order_ratings = [order["rating"] for order in orders if order["rating"] is not None]
    distinct_orders = {
        complaint["order_id"]
        for complaint in complaints
        if complaint["order_id"] is not None
    }
    valid_order_count = len(orders)
    orders_with_complaints = sum(
        order["id"] in distinct_orders for order in orders
    )
    resolved_durations = []
    for complaint in complaints:
        if complaint["status"] != "Resolved" or not complaint["resolved_at"]:
            continue
        try:
            created_at = datetime.fromisoformat(
                str(complaint["created_at"]).replace("Z", "+00:00")
            )
            resolved_at = datetime.fromisoformat(
                str(complaint["resolved_at"]).replace("Z", "+00:00")
            )
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            if resolved_at.tzinfo is None:
                resolved_at = resolved_at.replace(tzinfo=timezone.utc)
            if resolved_at >= created_at:
                resolved_durations.append(
                    (resolved_at - created_at).total_seconds() / 3600
                )
        except (TypeError, ValueError):
            continue
    suggestion_outcomes = [
        complaint
        for complaint in complaints
        if complaint["ml_first_suggestion_accepted"] is not None
    ]
    first_suggestion_resolved = sum(
        complaint["status"] == "Resolved"
        and complaint["ml_first_suggestion_accepted"] == 1
        for complaint in suggestion_outcomes
    )
    first_suggestion_denominator = len(suggestion_outcomes)
    first_response_outcomes = [
        complaint
        for complaint in complaints
        if complaint["ml_first_response_accepted"] is not None
    ]
    first_response_accepted = sum(
        complaint["ml_first_response_accepted"] == 1
        for complaint in first_response_outcomes
    )
    high_solution_ratings = sum(rating >= 4 for rating in all_complaint_ratings)
    expected_agreement = 0.0
    observed_agreement = 0.0
    if labels:
        label_pairs = [
            (
                normalize_category(row["annotator_one_category"]),
                normalize_category(row["annotator_two_category"]),
            )
            for row in labels
        ]
        observed_agreement = sum(first == second for first, second in label_pairs) / len(label_pairs)
        frequencies_one = Counter(first for first, _ in label_pairs)
        frequencies_two = Counter(second for _, second in label_pairs)
        expected_agreement = sum(
            frequencies_one[category] * frequencies_two[category]
            for category in COMPLAINT_CATEGORIES
        ) / (len(label_pairs) ** 2)
    kappa = (
        (observed_agreement - expected_agreement) / (1 - expected_agreement)
        if labels and expected_agreement < 1
        else 1.0 if labels and observed_agreement == 1
        else None
    )
    return jsonify(
        {
            "period_days": 90,
            "complaint_count": len(complaints),
            "complaints_per_100_orders": round(
                len(complaints) / valid_order_count * 100, 2
            )
            if valid_order_count
            else None,
            "orders_with_complaints": orders_with_complaints,
            "complaint_order_rate_ci": wilson_interval(
                orders_with_complaints,
                valid_order_count,
            ),
            "complaint_resolution_rate": round(
                sum(complaint["status"] == "Resolved" for complaint in complaints)
                / len(complaints),
                4,
            )
            if complaints
            else None,
            "complaint_resolution_rate_ci": wilson_interval(
                sum(complaint["status"] == "Resolved" for complaint in complaints),
                len(complaints),
            ),
            "median_resolution_hours": round(statistics.median(resolved_durations), 2)
            if resolved_durations
            else None,
            "first_suggestion_resolution_rate": round(
                first_suggestion_resolved / first_suggestion_denominator, 4
            )
            if first_suggestion_denominator
            else None,
            "first_suggestion_resolution_ci": wilson_interval(
                first_suggestion_resolved,
                first_suggestion_denominator,
            ),
            "first_suggestion_cases": first_suggestion_denominator,
            "first_response_acceptance_rate": round(
                first_response_accepted / len(first_response_outcomes), 4
            )
            if first_response_outcomes
            else None,
            "high_solution_rating_rate": round(
                high_solution_ratings / len(all_complaint_ratings), 4
            )
            if all_complaint_ratings
            else None,
            "high_solution_rating_ci": wilson_interval(
                high_solution_ratings,
                len(all_complaint_ratings),
            ),
            "labeling": {
                "double_labeled_examples": len(labels),
                "cohen_kappa": round(kappa, 4) if kappa is not None else None,
                "target": 0.6,
            },
            "average_solution_rating": round(
                statistics.fmean(all_complaint_ratings), 2
            )
            if all_complaint_ratings
            else None,
            "solution_ratings": len(all_complaint_ratings),
            "categories": category_summary,
            "weekly_patterns": spikes,
            "current_model": {
                "version": current_model["version"] if current_model else None,
                "metrics": json.loads(current_model["metrics"])
                if current_model
                else None,
                "enabled": ml_enabled,
            },
            "rating_trends": [
                {
                    "month": month,
                    "average_rating": round(statistics.fmean(ratings), 2),
                    "rating_count": len(ratings),
                }
                for month, ratings in sorted(
                    _group_ratings_by_month(complaints).items()
                )
            ],
            "order_performance": {
                "orders": len(orders),
                "completed_orders": completed_orders,
                "completion_rate": round(completed_orders / len(orders), 4)
                if orders
                else None,
                "average_order_rating": round(statistics.fmean(order_ratings), 2)
                if order_ratings
                else None,
                "order_ratings": len(order_ratings),
            },
        }
    )


def _group_ratings_by_month(complaints):
    grouped = {}
    for complaint in complaints:
        rating = complaint["customer_rating"]
        if rating is None:
            continue
        try:
            created_at = datetime.fromisoformat(
                str(complaint["customer_rating_at"] or complaint["created_at"]).replace(
                    "Z", "+00:00"
                )
            )
        except (TypeError, ValueError):
            continue
        month = created_at.strftime("%Y-%m")
        grouped.setdefault(month, []).append(rating)
    return grouped


@app.get("/api/manager/complaints/ml")
@require_role("manager")
def complaint_ml_status():
    try:
        with get_connection() as connection:
            records = complaint_ml_training_records(connection)
            model, current_model = load_complaint_ml_model(connection)
            enabled = complaint_ml_is_enabled(connection)
            versions = connection.execute(
                """
                SELECT version, metrics, created_at, deployed
                FROM complaint_ml_versions
                ORDER BY created_at DESC
                LIMIT 10
                """
            ).fetchall()
    except DATABASE_ERRORS:
        app.logger.exception("Could not load complaint ML status")
        return jsonify({"error": "Could not load complaint ML status"}), 500
    except (OSError, pickle.PickleError, RuntimeError):
        app.logger.exception("Could not load the active complaint classifier")
        return jsonify({"error": "Could not load the active complaint classifier"}), 500

    new_examples = len(records)
    if current_model:
        new_examples = sum(
            record["created_at"] > current_model["created_at"]
            for record in records
        )
    retraining_due = new_examples >= MINIMUM_TRAINING_EXAMPLES
    if current_model:
        try:
            trained_at = datetime.fromisoformat(
                current_model["created_at"].replace("Z", "+00:00")
            )
            if trained_at.tzinfo is None:
                trained_at = trained_at.replace(tzinfo=timezone.utc)
            retraining_due = retraining_due or (
                datetime.now(timezone.utc) - trained_at >= timedelta(days=30)
            )
        except (TypeError, ValueError):
            app.logger.exception("Invalid complaint ML model timestamp")
            return jsonify({"error": "The active model has an invalid timestamp"}), 500

    return jsonify(
        {
            "enabled": enabled,
            "scikit_learn_available": SKLEARN_AVAILABLE,
            "model_available": model is not None,
            "current_model": {
                "version": current_model["version"],
                "created_at": current_model["created_at"],
                "metrics": json.loads(current_model["metrics"]),
            }
            if current_model
            else None,
            "labelled_examples": len(records),
            "new_examples_since_training": new_examples,
            "minimum_examples": MINIMUM_TRAINING_EXAMPLES,
            "retraining_due": retraining_due,
            "categories": list(COMPLAINT_CATEGORIES),
            "versions": [
                {
                    "version": row["version"],
                    "created_at": row["created_at"],
                    "deployed": bool(row["deployed"]),
                    "metrics": json.loads(row["metrics"]),
                }
                for row in versions
            ],
        }
    )


@app.post("/api/manager/complaints/ml/enabled")
@require_role("manager")
def set_complaint_ml_enabled():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
        return jsonify({"error": "A boolean enabled value is required"}), 400
    try:
        with get_connection() as connection:
            connection.execute(
                """
                INSERT INTO complaint_ml_settings (setting_key, setting_value)
                VALUES ('enabled', ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value
                """,
                ("1" if payload["enabled"] else "0",),
            )
    except DATABASE_ERRORS:
        app.logger.exception("Could not update complaint ML setting")
        return jsonify({"error": "Could not update the ML setting"}), 500
    return jsonify({"enabled": payload["enabled"]})


@app.post("/api/manager/complaints/ml/train")
@require_role("manager")
def train_complaint_ml_model():
    if not SKLEARN_AVAILABLE:
        return jsonify(
            {"error": "scikit-learn is unavailable; model training is disabled."}
        ), 503
    artifact_path = None
    temporary_path = None
    try:
        with get_connection() as connection:
            records = complaint_ml_training_records(connection)
            _, current_model = load_complaint_ml_model(connection)
            fixed_test_setting = connection.execute(
                """
                SELECT setting_value FROM complaint_ml_settings
                WHERE setting_key = ?
                """,
                (TEST_SPLIT_KEY,),
            ).fetchone()
            fixed_test_keys = (
                json.loads(fixed_test_setting["setting_value"])
                if fixed_test_setting
                else None
            )
            if current_model:
                new_examples = sum(
                    record["created_at"] > current_model["created_at"]
                    for record in records
                )
                trained_at = datetime.fromisoformat(
                    current_model["created_at"].replace("Z", "+00:00")
                )
                if trained_at.tzinfo is None:
                    trained_at = trained_at.replace(tzinfo=timezone.utc)
                monthly_retrain_due = (
                    datetime.now(timezone.utc) - trained_at >= timedelta(days=30)
                )
                if (
                    new_examples < MINIMUM_TRAINING_EXAMPLES
                    and not monthly_retrain_due
                ):
                    return jsonify(
                        {
                            "error": (
                                "Retraining requires 50 new labelled examples or "
                                "30 days since the previous training."
                            ),
                            "new_examples": new_examples,
                        }
                    ), 409
            candidate = train_model(
                records,
                fixed_test_keys=fixed_test_keys,
                existing_model=current_model,
            )

            timestamp = datetime.now(timezone.utc).isoformat()
            version = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
            ML_MODEL_FOLDER.mkdir(parents=True, exist_ok=True)
            artifact_path = ML_MODEL_FOLDER / f"{version}.pkl"
            temporary_path = ML_MODEL_FOLDER / f".{version}.tmp"
            with temporary_path.open("wb") as model_file:
                pickle.dump(candidate, model_file, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(temporary_path, artifact_path)
            temporary_path = None
            relative_path = str(artifact_path.relative_to(BASE_DIR))
            serialized_test_keys = json.dumps(candidate["test_keys"])
            connection.execute(
                "UPDATE complaint_ml_versions SET deployed = 0"
            )
            connection.execute(
                """
                INSERT INTO complaint_ml_versions
                    (version, artifact_path, metrics, fixed_test_keys, created_at, deployed)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    version,
                    relative_path,
                    json.dumps(candidate["metrics"]),
                    serialized_test_keys,
                    timestamp,
                ),
            )
            for key, value in (
                (MODEL_VERSION_KEY, version),
                (MODEL_TRAINED_AT_KEY, timestamp),
            ):
                connection.execute(
                    """
                    INSERT INTO complaint_ml_settings (setting_key, setting_value)
                    VALUES (?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value
                    """,
                    (key, value),
                )
            if not fixed_test_setting:
                connection.execute(
                    """
                    INSERT INTO complaint_ml_settings (setting_key, setting_value)
                    VALUES (?, ?)
                    ON CONFLICT(setting_key) DO UPDATE SET
                        setting_value = excluded.setting_value
                    """,
                    (TEST_SPLIT_KEY, serialized_test_keys),
                )
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    except DATABASE_ERRORS:
        app.logger.exception("Could not save complaint ML model")
        if artifact_path and artifact_path.exists():
            artifact_path.unlink()
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
        return jsonify({"error": "Could not save the trained complaint model"}), 500
    except (ImportError, OSError, pickle.PickleError, RuntimeError):
        app.logger.exception("Could not train or persist complaint ML model")
        if artifact_path and artifact_path.exists():
            artifact_path.unlink()
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()
        return jsonify({"error": "Could not train the complaint model"}), 500

    app.logger.info(
        "Manager user %s deployed complaint ML model %s",
        g.current_user["id"],
        version,
    )
    return jsonify(
        {
            "version": version,
            "created_at": timestamp,
            "metrics": candidate["metrics"],
        }
    ), 201


@app.post("/api/manager/complaints/ml/rollback")
@require_role("manager")
def rollback_complaint_ml_model():
    payload = request.get_json(silent=True)
    version = str(payload.get("version", "")).strip() if isinstance(payload, dict) else ""
    if not version:
        return jsonify({"error": "A model version is required"}), 400
    try:
        with get_connection() as connection:
            selected = connection.execute(
                """
                SELECT version, artifact_path
                FROM complaint_ml_versions WHERE version = ?
                """,
                (version,),
            ).fetchone()
            if not selected:
                return jsonify({"error": "Model version not found"}), 404
            artifact_path = (BASE_DIR / selected["artifact_path"]).resolve()
            artifact_path.relative_to(ML_MODEL_FOLDER.resolve())
            with artifact_path.open("rb") as model_file:
                pickle.load(model_file)
            connection.execute("UPDATE complaint_ml_versions SET deployed = 0")
            connection.execute(
                "UPDATE complaint_ml_versions SET deployed = 1 WHERE version = ?",
                (version,),
            )
            connection.execute(
                """
                INSERT INTO complaint_ml_settings (setting_key, setting_value)
                VALUES (?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value
                """,
                (MODEL_VERSION_KEY, version),
            )
    except DATABASE_ERRORS:
        app.logger.exception("Could not roll back complaint ML model to %s", version)
        return jsonify({"error": "Could not roll back the complaint model"}), 500
    except (OSError, pickle.PickleError, RuntimeError, ValueError):
        app.logger.exception("Could not load complaint ML rollback version %s", version)
        return jsonify({"error": "The selected model version could not be loaded"}), 500
    return jsonify({"version": version, "rolled_back": True})


@app.post("/api/complaints/<int:complaint_id>/classification")
@require_role("manager")
def classify_manager_complaint(complaint_id):
    try:
        with get_connection() as connection:
            complaint = connection.execute(
                "SELECT id, description, message, category FROM complaints WHERE id = ?",
                (complaint_id,),
            ).fetchone()
            if not complaint:
                return jsonify({"error": "Complaint not found"}), 404
            if not complaint_ml_is_enabled(connection):
                return jsonify(
                    {
                        "category": None,
                        "confidence": 0.0,
                        "needs_manual_review": True,
                        "reason": "ML assistance is disabled.",
                    }
                )
            model, current_model = load_complaint_ml_model(connection)
            if model is None:
                return jsonify(
                    {
                        "category": None,
                        "confidence": 0.0,
                        "needs_manual_review": True,
                        "reason": "No trained complaint model is available.",
                    }
                )
            _, _, known_details = complaint_ml_context(connection, complaint_id)
            result = predict_with_model(
                model,
                redact_personal_details(
                    complaint["description"] or complaint["message"] or "",
                    known_details,
                ),
            )
            connection.execute(
                """
                UPDATE complaints
                SET ml_suggested_category = ?, ml_confidence = ?,
                    ml_model_version = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    result["category"],
                    result["confidence"],
                    current_model["version"],
                    datetime.now(timezone.utc).isoformat(),
                    complaint_id,
                ),
            )
    except DATABASE_ERRORS:
        app.logger.exception("Could not classify complaint %s", complaint_id)
        return jsonify({"error": "Could not classify this complaint"}), 500
    except (ImportError, OSError, pickle.PickleError, RuntimeError):
        app.logger.exception("Could not load classifier for complaint %s", complaint_id)
        return jsonify({"error": "The local complaint classifier is unavailable"}), 503
    return jsonify(
        {
            **result,
            "current_category": complaint["category"],
            "model": current_model["version"],
        }
    )


@app.post("/api/complaints/<int:complaint_id>/category")
@require_role("manager")
def correct_complaint_category(complaint_id):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "A category correction is required"}), 400
    category = parse_category_label(payload.get("category"))
    if category is None:
        return jsonify({"error": "Choose a supported complaint category"}), 400

    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        with get_connection() as connection:
            complaint = connection.execute(
                """
                SELECT id, description, message, ml_suggested_category,
                       customer_name, customer_email
                FROM complaints WHERE id = ?
                """,
                (complaint_id,),
            ).fetchone()
            if not complaint:
                return jsonify({"error": "Complaint not found"}), 404
            text = redact_personal_details(
                complaint["description"] or complaint["message"] or "",
                (complaint["customer_name"], complaint["customer_email"]),
            ).strip()
            if not text:
                return jsonify({"error": "This complaint has no text to label"}), 400
            connection.execute(
                """
                UPDATE complaints
                SET ml_corrected_category = ?, ml_category_corrected_at = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (category, timestamp, timestamp, complaint_id),
            )
            connection.execute(
                """
                INSERT INTO complaint_ml_labels
                    (complaint_id, text, category, source, created_at)
                VALUES (?, ?, ?, 'local_correction', ?)
                ON CONFLICT(complaint_id) DO UPDATE SET
                    text = excluded.text,
                    category = excluded.category,
                    source = excluded.source,
                    created_at = excluded.created_at
                """,
                (complaint_id, text, category, timestamp),
            )
    except DATABASE_ERRORS:
        app.logger.exception("Could not save category correction for complaint %s", complaint_id)
        return jsonify({"error": "Could not save the category correction"}), 500
    return jsonify({"category": category, "saved_as_training_example": True})


@app.post("/api/complaints/<int:complaint_id>/proposal")
@require_role("manager")
def propose_complaint_solution(complaint_id):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "A valid proposal request is required"}), 400
    use_gemini = payload.get("use_gemini") is True
    prediction = None
    current_model = None
    try:
        with get_connection() as connection:
            complaint = connection.execute(
                """
                SELECT id, description, message, ml_suggested_category,
                       ml_confidence, ml_model_version, customer_name,
                       customer_email
                FROM complaints WHERE id = ?
                """,
                (complaint_id,),
            ).fetchone()
            if not complaint:
                return jsonify({"error": "Complaint not found"}), 404
            text = redact_personal_details(
                complaint["description"] or complaint["message"] or "",
                (complaint["customer_name"], complaint["customer_email"]),
            )
            _, solutions, known_details = complaint_ml_context(connection, complaint_id)
            if complaint_ml_is_enabled(connection):
                try:
                    model, current_model = load_complaint_ml_model(connection)
                    if model is not None:
                        prediction = predict_with_model(model, text)
                        if not prediction["needs_manual_review"]:
                            timestamp = datetime.now(timezone.utc).isoformat()
                            connection.execute(
                                """
                                UPDATE complaints
                                SET ml_suggested_category = ?, ml_confidence = ?,
                                    ml_model_version = ?, updated_at = ?
                                WHERE id = ?
                                """,
                                (
                                    prediction["category"],
                                    prediction["confidence"],
                                    current_model["version"],
                                    timestamp,
                                    complaint_id,
                                ),
                            )
                except (ImportError, OSError, pickle.PickleError, RuntimeError, ValueError) as error:
                    app.logger.warning(
                        "Complaint model unavailable for solution proposal %s: %s",
                        complaint_id,
                        error,
                    )
                    prediction = None
                    current_model = None
            predicted_category = (
                prediction["category"]
                if prediction and not prediction["needs_manual_review"]
                else None
            )
            if predicted_category:
                _, category_solutions, known_details = complaint_ml_context(
                    connection, complaint_id, predicted_category
                )
                if category_solutions:
                    solutions = category_solutions
            similar_solutions = retrieve_solutions(
                text,
                solutions,
                known_details=known_details,
                predicted_category=predicted_category,
            )
    except DATABASE_ERRORS:
        app.logger.exception("Could not retrieve solutions for complaint %s", complaint_id)
        return jsonify({"error": "Could not retrieve past solutions"}), 500

    proposal = similar_solutions[0]["solution"] if similar_solutions else ""
    source = "local_history" if proposal else "manual"
    warning = None

    if use_gemini:
        try:
            proposal = draft_with_gemini(
                text,
                similar_solutions,
                GEMINI_API_KEY,
                GEMINI_MODEL,
            )
            source = "gemini"
        except (RuntimeError, ValueError) as error:
            app.logger.warning(
                "Gemini drafting unavailable for complaint %s: %s",
                complaint_id,
                error,
            )
            warning = (
                "AI drafting is unavailable. Configure GEMINI_API_KEY in the backend "
                "environment; showing the closest past solution instead."
                if not GEMINI_API_KEY
                else f"AI drafting failed ({error}); showing the closest past solution instead."
            )

    if proposal:
        try:
            with get_connection() as connection:
                connection.execute(
                    """
                    UPDATE complaints
                    SET ml_suggested_response = ?, ml_first_suggestion_at = ?,
                        updated_at = ?
                    WHERE id = ? AND ml_first_suggestion_at IS NULL
                    """,
                    (
                        proposal,
                        datetime.now(timezone.utc).isoformat(),
                        datetime.now(timezone.utc).isoformat(),
                        complaint_id,
                    ),
                )
        except DATABASE_ERRORS:
            app.logger.exception(
                "Could not record the first suggested response for complaint %s",
                complaint_id,
            )
            return jsonify({"error": "Could not record the solution suggestion"}), 500

    return jsonify(
        {
            "proposal": proposal,
            "source": source,
            "warning": warning,
            "similar_solutions": similar_solutions,
            "category": prediction["category"] if prediction else None,
            "confidence": prediction["confidence"] if prediction else None,
            "model": current_model["version"] if current_model else None,
            "human_approval_required": True,
            "sent_to_customer": False,
        }
    )


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
                email_error = NULL, ml_first_response_accepted = CASE
                    WHEN ml_first_response_accepted IS NULL
                         AND ml_suggested_response IS NOT NULL
                    THEN CASE WHEN ml_suggested_response = ? THEN 1 ELSE 0 END
                    ELSE ml_first_response_accepted
                END,
                ml_first_suggestion_accepted = CASE
                    WHEN ml_first_suggestion_accepted IS NULL
                         AND ml_suggested_response IS NOT NULL
                    THEN CASE WHEN ml_suggested_response = ? THEN 1 ELSE 0 END
                    ELSE ml_first_suggestion_accepted
                END,
                ml_first_response_at = CASE
                    WHEN ml_first_response_at IS NULL
                         AND ml_suggested_response IS NOT NULL THEN ?
                    ELSE ml_first_response_at
                END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                response_text,
                timestamp,
                response_text,
                response_text,
                timestamp,
                timestamp,
                complaint_id,
            ),
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


def send_complaint_email(complaint, feedback_token):
    if not PUBLIC_APP_URL:
        raise ValueError(
            "Set BUNDU_PUBLIC_URL to the public HTTPS application URL before sending "
            "complaint responses."
        )
    username = os.environ.get("SMTP_USER", "").strip()
    sender = os.environ.get("SMTP_FROM", "").strip() or username
    reference = complaint["reference"] or f"CMP-{complaint['id']}"
    customer_name = complaint["customer_name"].strip() or "DineDesk customer"
    response_text = complaint["manager_response"]
    feedback_url = f"{PUBLIC_APP_URL}/feedback/{feedback_token}"
    message = EmailMessage()
    message["Subject"] = f"Update regarding your DineDesk complaint {reference}"
    message["From"] = sender
    message["To"] = complaint["customer_email"]
    message.set_content(
        f"Hello {customer_name},\n\n"
        f"Thank you for contacting DineDesk about complaint {reference}.\n\n"
        f"Response from our manager:\n{response_text}\n\n"
        f"Rate this solution: {feedback_url}\n\n"
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
              <p style="margin-top:24px"><a href="{escape(feedback_url, quote=True)}">Rate this solution</a></p>
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
    if not PUBLIC_APP_URL:
        return jsonify({
            "error": (
                "Set BUNDU_PUBLIC_URL to the public HTTPS application URL before "
                "sending complaint responses."
            )
        }), 503
    timestamp = datetime.now(timezone.utc).isoformat()
    feedback_token = secrets.token_urlsafe(32)
    feedback_token_hash = hashlib.sha256(feedback_token.encode("utf-8")).hexdigest()
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
                email_started_at = ?, feedback_token_hash = ?, updated_at = ?
            WHERE id = ? AND sent_at IS NULL
              AND (email_started_at IS NULL OR email_started_at < ?)
            """,
            (timestamp, feedback_token_hash, timestamp, complaint_id, stale_before),
        )
        if reserved.rowcount != 1:
            return jsonify({
                "error": "A response email is already being sent. Please wait and refresh."
            }), 409
        complaint = connection.execute(
            "SELECT * FROM complaints WHERE id = ?", (complaint_id,)
        ).fetchone()

    try:
        send_complaint_email(complaint, feedback_token)
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