import gc
import hashlib
import hmac
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

_environment = {
    name: os.environ.get(name)
    for name in (
        "BUNDU_ENV",
        "BUNDU_JWT_SECRET",
        "BUNDU_DATABASE_URL",
        "BUNDU_SKIP_DATABASE_INIT",
        "BUNDU_MANAGER_REGISTRATION_KEY",
        "PAYSTACK_SECRET_KEY",
        "PAYSTACK_CURRENCY",
        "BUNDU_FRONTEND_URL",
    )
}
os.environ.update(
    {
        "BUNDU_ENV": "development",
        "BUNDU_JWT_SECRET": "registration-test-secret-that-is-long-enough",
        "BUNDU_DATABASE_URL": "",
        "BUNDU_SKIP_DATABASE_INIT": "1",
        "BUNDU_MANAGER_REGISTRATION_KEY": "initial-manager-bootstrap-test-key",
    }
)
try:
    import Backend.app as backend
    import Backend.database as database
finally:
    for name, value in _environment.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


class StaffRegistrationInvitationKeyTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.original_values = {
            "database_path": backend.DATABASE_PATH,
            "database_engine": backend.DATABASE_ENGINE,
            "database_url": database.DATABASE_URL,
            "is_development": backend.IS_DEVELOPMENT,
            "manager_key": backend.MANAGER_REGISTRATION_KEY,
            "paystack_secret_key": backend.PAYSTACK_SECRET_KEY,
            "paystack_currency": backend.PAYSTACK_CURRENCY,
            "frontend_url": backend.FRONTEND_URL,
            "testing": backend.app.config.get("TESTING"),
        }
        backend.configure_database(None)
        backend.DATABASE_ENGINE = "sqlite"
        backend.DATABASE_PATH = Path(self.temporary_directory.name) / "registration-test.db"
        backend.IS_DEVELOPMENT = True
        backend.MANAGER_REGISTRATION_KEY = "initial-manager-bootstrap-test-key"
        backend.PAYSTACK_SECRET_KEY = "paystack-unit-test-secret"
        backend.PAYSTACK_CURRENCY = "ZAR"
        backend.FRONTEND_URL = "http://127.0.0.1:5500"
        backend.app.config["TESTING"] = True
        backend.initialise_database(seed_menu=False)
        with backend.get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users
                    (firstname, lastname, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    "Test",
                    "Manager",
                    "manager@example.com",
                    "unused-test-hash",
                    "manager",
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        self.manager_id = cursor.lastrowid
        self.manager_token = backend.create_token(
            {"id": self.manager_id, "email": "manager@example.com", "role": "manager"},
            "registration-test-session",
        )
        self.request_counter = 0
        self.client = backend.app.test_client()

    def test_database_initialization_does_not_seed_menu_items(self):
        backend.initialise_database()
        with backend.get_connection() as connection:
            menu_count = connection.execute(
                "SELECT COUNT(*) AS count FROM menu_items"
            ).fetchone()["count"]

        self.assertEqual(menu_count, 0)

    def tearDown(self):
        backend.DATABASE_PATH = self.original_values["database_path"]
        backend.DATABASE_ENGINE = self.original_values["database_engine"]
        backend.configure_database(self.original_values["database_url"])
        backend.IS_DEVELOPMENT = self.original_values["is_development"]
        backend.MANAGER_REGISTRATION_KEY = self.original_values["manager_key"]
        backend.PAYSTACK_SECRET_KEY = self.original_values["paystack_secret_key"]
        backend.PAYSTACK_CURRENCY = self.original_values["paystack_currency"]
        backend.FRONTEND_URL = self.original_values["frontend_url"]
        backend.app.config["TESTING"] = self.original_values["testing"]
        gc.collect()
        self.temporary_directory.cleanup()

    def _create_invitation(self, email, role):
        self.request_counter += 1
        return self.client.post(
            "/api/staff/invitations",
            headers={"Authorization": f"Bearer {self.manager_token}"},
            json={"email": email, "role": role},
            environ_overrides={"REMOTE_ADDR": f"192.0.2.{self.request_counter}"},
        )

    def _register(self, role, email, key):
        self.request_counter += 1
        return self.client.post(
            "/api/auth/register",
            json={
                "firstname": "Taylor",
                "lastname": "Staff",
                "email": email,
                "password": "valid-registration-password",
                "role": role,
                "registration_key": key,
            },
            environ_overrides={"REMOTE_ADDR": f"192.0.2.{self.request_counter}"},
        )

    def _create_staff(self, payload):
        self.request_counter += 1
        return self.client.post(
            "/api/staff",
            headers={"Authorization": f"Bearer {self.manager_token}"},
            json=payload,
            environ_overrides={"REMOTE_ADDR": f"192.0.2.{self.request_counter}"},
        )

    def _account_count(self):
        with backend.get_connection() as connection:
            return connection.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def _create_public_menu_item(self):
        with backend.get_connection() as connection:
            connection.execute(
                """
                INSERT INTO menu_items (name, category, description, price, icon, is_public)
                VALUES (?, ?, ?, ?, ?, 1)
                """,
                ("Test meal", "Mains", "Test description", 12.34, "meal"),
            )

    def _online_order(self):
        self._create_public_menu_item()
        self.request_counter += 1
        with patch.object(
            backend,
            "initialize_paystack_transaction",
            side_effect=lambda order, reference: {
                "reference": reference,
                "url": "https://checkout.paystack.com/test-access-code",
            },
        ):
            response = self.client.post(
                "/api/orders",
                json={
                    "customer_email": "customer@example.com",
                    "order_type": "Takeaway",
                    "payment_method": "online",
                    "items": [{"name": "Test meal", "quantity": 1, "price": 0.01}],
                },
                environ_overrides={"REMOTE_ADDR": f"198.51.100.{self.request_counter}"},
            )
        return response

    def _paystack_webhook(self, event):
        payload = json.dumps(event).encode("utf-8")
        signature = hmac.new(
            backend.PAYSTACK_SECRET_KEY.encode("utf-8"),
            payload,
            hashlib.sha512,
        ).hexdigest()
        return self.client.post(
            "/api/payments/paystack/webhook",
            data=payload,
            headers={"x-paystack-signature": signature},
        )

    def test_invitation_is_bound_to_email_role_and_consumed_once(self):
        email = "waiter@example.com"
        invitation_response = self._create_invitation(email, "waiter")
        self.assertEqual(invitation_response.status_code, 201)
        invitation = invitation_response.get_json()
        key = invitation["invitation_key"]
        self.assertEqual(invitation["email"], email)
        self.assertEqual(invitation["role"], "waiter")

        baseline = self._account_count()
        self.assertEqual(self._register("waiter", email, "wrong-key-value-that-is-long").status_code, 403)
        self.assertEqual(
            self._register(
                "waiter",
                "shared-key@example.com",
                backend.MANAGER_REGISTRATION_KEY,
            ).status_code,
            403,
        )
        self.assertEqual(self._account_count(), baseline)
        self.assertEqual(self._register("waiter", "other@example.com", key).status_code, 403)
        self.assertEqual(self._register("manager", email, key).status_code, 403)
        self.assertEqual(self._account_count(), baseline)

        self.assertEqual(self._register("waiter", email, key).status_code, 201)
        self.assertEqual(self._register("waiter", email, key).status_code, 403)
        self.assertEqual(self._account_count(), baseline + 1)

        with backend.get_connection() as connection:
            record = connection.execute(
                "SELECT key_hash, status FROM staff_invitations WHERE email = ?",
                (email,),
            ).fetchone()
        self.assertNotEqual(record["key_hash"], key)
        self.assertEqual(record["status"], "used")

    def test_manager_can_issue_invitation_for_another_manager(self):
        email = "new-manager@example.com"
        invitation = self._create_invitation(email, "manager").get_json()

        response = self._register("manager", email, invitation["invitation_key"])

        self.assertEqual(response.status_code, 201)

    def test_new_invitation_replaces_the_previous_unused_key(self):
        email = "replace@example.com"
        old_key = self._create_invitation(email, "waiter").get_json()["invitation_key"]
        new_key = self._create_invitation(email, "waiter").get_json()["invitation_key"]

        self.assertEqual(self._register("waiter", email, old_key).status_code, 403)
        self.assertEqual(self._register("waiter", email, new_key).status_code, 201)

    def test_expired_invitation_is_rejected(self):
        email = "expired@example.com"
        key = self._create_invitation(email, "waiter").get_json()["invitation_key"]
        with backend.get_connection() as connection:
            connection.execute(
                "UPDATE staff_invitations SET expires_at = ? WHERE email = ?",
                ("2000-01-01T00:00:00+00:00", email),
            )

        self.assertEqual(self._register("waiter", email, key).status_code, 403)
        self.assertEqual(self._account_count(), 1)

    def test_shared_manager_key_only_bootstraps_first_manager(self):
        with backend.get_connection() as connection:
            connection.execute("UPDATE users SET is_active = 0 WHERE id = ?", (self.manager_id,))

        first_manager = self._register(
            "manager", "first-manager@example.com", backend.MANAGER_REGISTRATION_KEY
        )
        self.assertEqual(first_manager.status_code, 201)
        second_manager = self._register(
            "manager", "second-manager@example.com", backend.MANAGER_REGISTRATION_KEY
        )
        self.assertEqual(second_manager.status_code, 403)

    def test_staff_password_must_be_generated_separately(self):
        staff = {
            "firstname": "Waiter",
            "lastname": "Example",
            "email": "waiter@example.com",
            "role": "waiter",
            "hourly_rate": 35,
        }
        rejected = self._create_staff({**staff, "password": "manager-chosen-password"})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(self._account_count(), 1)

        created = self._create_staff(staff)
        self.assertEqual(created.status_code, 201)
        self.assertNotIn("password", created.get_json())
        self.assertEqual(self._account_count(), 2)

    def test_online_checkout_is_disabled_without_paystack_configuration(self):
        self._create_public_menu_item()
        backend.PAYSTACK_SECRET_KEY = ""
        self.request_counter += 1
        response = self.client.post(
            "/api/orders",
            json={
                "customer_email": "customer@example.com",
                "order_type": "Takeaway",
                "payment_method": "online",
                "items": [{"name": "Test meal", "quantity": 1}],
            },
            environ_overrides={"REMOTE_ADDR": f"198.51.100.{self.request_counter}"},
        )

        self.assertEqual(response.status_code, 503)
        with backend.get_connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(count, 0)

    def test_order_history_email_is_optional_for_in_person_orders(self):
        self._create_public_menu_item()
        self.request_counter += 1
        response = self.client.post(
            "/api/orders",
            json={
                "order_type": "Takeaway",
                "payment_method": "cash",
                "items": [{"name": "Test meal", "quantity": 1}],
            },
            environ_overrides={"REMOTE_ADDR": f"198.51.100.{self.request_counter}"},
        )

        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json()["customer_email"], "Not provided")

        self.request_counter += 1
        history_response = self.client.post(
            "/api/customer/order-history/request-code",
            json={"email": "Not provided"},
            environ_overrides={"REMOTE_ADDR": f"198.51.100.{self.request_counter}"},
        )
        self.assertEqual(history_response.status_code, 400)

        self.request_counter += 1
        identified_response = self.client.post(
            "/api/orders",
            json={
                "customer_email": "customer@example.com",
                "order_type": "Takeaway",
                "payment_method": "cash",
                "items": [{"name": "Test meal", "quantity": 1}],
            },
            environ_overrides={"REMOTE_ADDR": f"198.51.100.{self.request_counter}"},
        )
        self.assertEqual(identified_response.status_code, 201)
        self.assertEqual(identified_response.get_json()["customer_email"], "customer@example.com")

        self.request_counter += 1
        online_response = self.client.post(
            "/api/orders",
            json={
                "order_type": "Takeaway",
                "payment_method": "online",
                "items": [{"name": "Test meal", "quantity": 1}],
            },
            environ_overrides={"REMOTE_ADDR": f"198.51.100.{self.request_counter}"},
        )
        self.assertEqual(online_response.status_code, 400)
        with backend.get_connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        self.assertEqual(count, 2)

    def test_paystack_initialization_uses_server_price_and_callback(self):
        session_response = MagicMock()
        session_response.__enter__.return_value.read.return_value = json.dumps(
            {
                "status": True,
                "data": {
                    "reference": "BDU-17-test-reference",
                    "authorization_url": "https://checkout.paystack.com/test-access-code",
                },
            }
        ).encode("utf-8")
        order = {
            "id": 17,
            "order_number": "BDU-00017",
            "customer_email": "customer@example.com",
            "total": 24.68,
        }

        with patch.object(backend, "urlopen", return_value=session_response) as urlopen:
            result = backend.initialize_paystack_transaction(
                order,
                "BDU-17-test-reference",
            )

        self.assertEqual(result["reference"], "BDU-17-test-reference")
        self.assertEqual(
            result["url"],
            "https://checkout.paystack.com/test-access-code",
        )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.paystack.co/transaction/initialize")
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(
            payload["callback_url"],
            "http://127.0.0.1:5500/?order=17&checkout=paystack&reference=BDU-17-test-reference",
        )
        self.assertEqual(payload["amount"], 2468)
        self.assertEqual(payload["currency"], "ZAR")
        self.assertEqual(payload["reference"], "BDU-17-test-reference")
        self.assertEqual(payload["metadata"]["order_id"], "17")

    def test_online_order_is_hidden_until_valid_paid_webhook(self):
        response = self._online_order()
        self.assertEqual(response.status_code, 201)
        order = response.get_json()
        self.assertEqual(order["payment_method"], "online")
        self.assertEqual(order["payment_status"], "pending")
        self.assertEqual(order["status"], "Awaiting payment")
        self.assertEqual(order["payment_provider"], "paystack")
        self.assertTrue(order["checkout_url"].startswith("https://checkout.paystack.com/"))

        staff_response = self.client.get(
            "/api/orders?active=true",
            headers={"Authorization": f"Bearer {self.manager_token}"},
        )
        self.assertEqual(staff_response.status_code, 200)
        self.assertEqual(staff_response.get_json(), [])

        event = {
            "event": "charge.success",
            "data": {
                "reference": order["payment_reference"],
                "status": "success",
                "amount": 1234,
                "currency": "ZAR",
            },
        }
        mismatch_response = self._paystack_webhook(
            {
                **event,
                "data": {
                    **event["data"],
                    "amount": 1233,
                },
            }
        )
        self.assertEqual(mismatch_response.status_code, 400)
        invalid_signature_response = self.client.post(
            "/api/payments/paystack/webhook",
            data=json.dumps(event),
            headers={"x-paystack-signature": "invalid"},
        )
        self.assertEqual(invalid_signature_response.status_code, 400)

        paid_response = self._paystack_webhook(event)
        self.assertEqual(paid_response.status_code, 200)
        self.assertEqual(self._paystack_webhook(event).status_code, 200)
        with backend.get_connection() as connection:
            paid_order = connection.execute(
                "SELECT status, payment_status FROM orders WHERE id = ?",
                (order["id"],),
            ).fetchone()
        self.assertEqual(paid_order["status"], "Received")
        self.assertEqual(paid_order["payment_status"], "paid")

        visible_orders = self.client.get(
            "/api/orders?active=true",
            headers={"Authorization": f"Bearer {self.manager_token}"},
        ).get_json()
        self.assertEqual([item["id"] for item in visible_orders], [order["id"]])

    def test_customer_can_verify_paystack_payment_after_checkout_return(self):
        order = self._online_order().get_json()
        verified_transaction = {
            "reference": order["payment_reference"],
            "status": "success",
            "amount": 1234,
            "currency": "ZAR",
        }

        with patch.object(
            backend,
            "paystack_api_request",
            return_value=verified_transaction,
        ) as verify_transaction:
            response = self.client.post(
                f"/api/orders/{order['id']}/paystack/verify",
                headers={"X-Order-Tracking-Token": order["tracking_token"]},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["payment_status"], "paid")
        verify_transaction.assert_called_once_with(
            f"/transaction/verify/{order['payment_reference']}"
        )

    def test_malformed_paystack_webhook_shape_is_rejected(self):
        response = self._paystack_webhook(
            {
                "event": "charge.success",
                "data": [],
            }
        )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(backend.paystack_signature_is_valid(b"payload", "invalid"))

    def _create_waiter(self, email):
        with backend.get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users
                    (firstname, lastname, email, password_hash, role, created_at)
                VALUES (?, ?, ?, ?, 'waiter', ?)
                """,
                ("Test", "Waiter", email, "unused-test-hash", datetime.now(timezone.utc).isoformat()),
            )
        waiter_id = cursor.lastrowid
        token = backend.create_token(
            {"id": waiter_id, "email": email, "role": "waiter"},
            f"waiter-test-session-{waiter_id}",
        )
        return waiter_id, {"Authorization": f"Bearer {token}"}

    def _create_served_order(
        self,
        waiter_id,
        *,
        served_hours_ago=3,
        order_type="Dine in",
        payment_status="unpaid",
        payment_requested=0,
    ):
        now = datetime.now(timezone.utc)
        served_at = (now - timedelta(hours=served_hours_ago)).isoformat()
        with backend.get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO orders
                    (customer_name, customer_email, items, total, status, created_at,
                     updated_at, payment_method, payment_provider, payment_status,
                     order_type, waiter_id, payment_requested, served_at)
                VALUES (?, ?, ?, ?, 'Served', ?, ?, 'cash', 'in_person', ?, ?, ?, ?, ?)
                """,
                (
                    "Test Customer",
                    "customer@example.com",
                    json.dumps([{"name": "Test meal", "quantity": 1, "price": 12.34}]),
                    12.34,
                    served_at,
                    served_at,
                    payment_status,
                    order_type,
                    waiter_id,
                    payment_requested,
                    served_at,
                ),
            )
        return cursor.lastrowid

    def test_waiter_cancellation_requires_overdue_unpaid_assigned_dine_in_order(self):
        waiter_id, waiter_headers = self._create_waiter("assigned-waiter@example.com")
        _, other_waiter_headers = self._create_waiter("other-waiter@example.com")

        recent_order = self._create_served_order(waiter_id, served_hours_ago=1.5)
        recent_response = self.client.patch(
            f"/api/orders/{recent_order}/cancel", headers=waiter_headers
        )
        self.assertEqual(recent_response.status_code, 409)

        assigned_order = self._create_served_order(waiter_id)
        wrong_waiter_response = self.client.patch(
            f"/api/orders/{assigned_order}/cancel", headers=other_waiter_headers
        )
        self.assertEqual(wrong_waiter_response.status_code, 403)

        payment_requested_order = self._create_served_order(
            waiter_id, payment_requested=1
        )
        payment_requested_response = self.client.patch(
            f"/api/orders/{payment_requested_order}/cancel", headers=waiter_headers
        )
        self.assertEqual(payment_requested_response.status_code, 200)

        paid_order = self._create_served_order(waiter_id, payment_status="paid")
        paid_response = self.client.patch(
            f"/api/orders/{paid_order}/cancel", headers=waiter_headers
        )
        self.assertEqual(paid_response.status_code, 409)

        takeaway_order = self._create_served_order(waiter_id, order_type="Takeaway")
        takeaway_response = self.client.patch(
            f"/api/orders/{takeaway_order}/cancel", headers=waiter_headers
        )
        self.assertEqual(takeaway_response.status_code, 409)

        generic_cancel_response = self.client.patch(
            f"/api/orders/{assigned_order}",
            headers=waiter_headers,
            json={"status": "Cancelled"},
        )
        self.assertEqual(generic_cancel_response.status_code, 409)

        cancelled_response = self.client.patch(
            f"/api/orders/{assigned_order}/cancel", headers=waiter_headers
        )
        self.assertEqual(cancelled_response.status_code, 200)
        self.assertEqual(cancelled_response.get_json()["status"], "Cancelled")
        self.assertEqual(
            cancelled_response.get_json()["cancellation_reason"],
            "Served for over two hours without collection",
        )
        reopen_response = self.client.patch(
            f"/api/orders/{assigned_order}",
            headers=waiter_headers,
            json={"status": "Received"},
        )
        self.assertEqual(reopen_response.status_code, 409)

    def test_served_timestamp_is_set_when_order_transitions_to_served(self):
        waiter_id, waiter_headers = self._create_waiter("served-waiter@example.com")
        with backend.get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO orders
                    (customer_name, customer_email, items, total, status, created_at,
                     updated_at, payment_method, order_type, waiter_id)
                VALUES (?, ?, ?, ?, 'Ready', ?, ?, 'cash', 'Dine in', ?)
                """,
                (
                    "Test Customer",
                    "customer@example.com",
                    json.dumps([{"name": "Test meal", "quantity": 1, "price": 12.34}]),
                    12.34,
                    datetime.now(timezone.utc).isoformat(),
                    datetime.now(timezone.utc).isoformat(),
                    waiter_id,
                ),
            )
        order_id = cursor.lastrowid

        response = self.client.patch(
            f"/api/orders/{order_id}",
            headers=waiter_headers,
            json={"status": "Served"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["served_at"])

    def test_manager_complaint_count_decreases_when_complaint_is_opened(self):
        timestamp = datetime.now(timezone.utc).isoformat()
        with backend.get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO complaints
                    (customer_name, customer_email, message, status, created_at, updated_at)
                VALUES (?, ?, ?, 'Open', ?, ?)
                """,
                ("Test Customer", "customer@example.com", "Test complaint", timestamp, timestamp),
            )
        complaint_id = cursor.lastrowid
        manager_headers = {"Authorization": f"Bearer {self.manager_token}"}

        count_response = self.client.get(
            "/api/manager/complaints/unread-count", headers=manager_headers
        )
        self.assertEqual(count_response.status_code, 200)
        self.assertEqual(count_response.get_json()["count"], 1)

        opened_response = self.client.post(
            f"/api/complaints/{complaint_id}/open", headers=manager_headers
        )
        self.assertEqual(opened_response.status_code, 200)
        viewed_at = opened_response.get_json()["manager_viewed_at"]
        self.assertTrue(viewed_at)

        repeated_open_response = self.client.post(
            f"/api/complaints/{complaint_id}/open", headers=manager_headers
        )
        self.assertEqual(
            repeated_open_response.get_json()["manager_viewed_at"], viewed_at
        )
        self.assertEqual(
            self.client.get(
                "/api/manager/complaints/unread-count", headers=manager_headers
            ).get_json()["count"],
            0,
        )

    def test_daily_usage_deducts_stock_and_exposes_reorder_forecast(self):
        manager_headers = {"Authorization": f"Bearer {self.manager_token}"}
        with backend.get_connection() as connection:
            cursor = connection.execute(
                """
                INSERT INTO pantry_items
                    (name, category, quantity, unit, reorder_level, cost_per_unit, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "Test rice",
                    "Dry Goods",
                    10,
                    "kg",
                    3,
                    2.5,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        item_id = cursor.lastrowid
        usage_date = datetime.now(backend.SOUTH_AFRICA_TIMEZONE).date().isoformat()
        future_date = (
            datetime.now(backend.SOUTH_AFRICA_TIMEZONE).date() + timedelta(days=1)
        ).isoformat()

        future_usage_response = self.client.post(
            f"/api/pantry/{item_id}/adjust",
            headers=manager_headers,
            json={"quantity": 1, "reason": "Usage", "date": future_date},
        )
        self.assertEqual(future_usage_response.status_code, 400)

        usage_response = self.client.post(
            f"/api/pantry/{item_id}/adjust",
            headers=manager_headers,
            json={"quantity": 4, "reason": "Usage", "date": usage_date},
        )
        self.assertEqual(usage_response.status_code, 200)
        self.assertEqual(usage_response.get_json()["quantity"], 6)

        pantry_response = self.client.get("/api/pantry", headers=manager_headers)
        self.assertEqual(pantry_response.status_code, 200)
        item = next(
            item for item in pantry_response.get_json()["items"] if item["id"] == item_id
        )
        self.assertEqual(item["used_today"], 4)
        self.assertAlmostEqual(item["average_daily_usage"], 4 / 7)
        self.assertAlmostEqual(item["days_until_reorder"], 5.25)

        with backend.get_connection() as connection:
            movement = connection.execute(
                "SELECT quantity_change, reason, movement_date FROM stock_movements WHERE item_id = ?",
                (item_id,),
            ).fetchone()
        self.assertEqual(movement["quantity_change"], -4)
        self.assertEqual(movement["reason"], "Usage")
        self.assertEqual(movement["movement_date"], usage_date)


if __name__ == "__main__":
    unittest.main()
