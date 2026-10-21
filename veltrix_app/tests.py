import os
import subprocess
import sys
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode
from rest_framework import status
from rest_framework.test import APIClient

from veltrix_app.api.permissions import IsSupportStaff
from veltrix_app.models import Conversation, DemoAccount, Instrument, Message, Position, Transaction
from veltrix_app.utils.email import make_verification_token

User = get_user_model()


class HealthAndMarketAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_health_check(self):
        response = self.client.get("/api/health/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json().get("status"), "ok")

    def test_instrument_list(self):
        Instrument.objects.create(
            symbol="AAPL",
            name="Apple Inc.",
            asset_class="Equity",
            currency="USD",
            price=Decimal("214.42"),
            change_percent=Decimal("0.62"),
            is_active=True,
        )
        response = self.client.get("/api/instruments/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertTrue(any(item["symbol"] == "AAPL" for item in data))


class UserAuthAndRegistrationTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_register_creates_user_and_demo_account(self):
        payload = {
            "email": "trader@example.com",
            "password": "StrongPassword123!",
            "first_name": "Trader",
            "last_name": "One",
        }
        response = self.client.post("/api/auth/register/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        user = User.objects.get(email="trader@example.com")
        self.assertEqual(user.first_name, "Trader")
        self.assertEqual(user.role, User.Role.CLIENT)

        # Check DemoAccount
        account = DemoAccount.objects.get(user=user)
        self.assertEqual(account.currency, "USD")
        self.assertEqual(account.cash_balance, Decimal("0.00"))

    def test_jwt_token_obtain(self):
        User.objects.create_user(
            username="investor@example.com",
            email="investor@example.com",
            password="SecurePass123!",
        )
        response = self.client.post(
            "/api/auth/token/",
            data={"username": "investor@example.com", "password": "SecurePass123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("access", response.json())
        self.assertIn("refresh", response.json())

    def test_registration_requires_email(self):
        response = self.client.post(
            "/api/auth/register/",
            data={"password": "StrongPassword123!"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_registration_rejects_weak_password(self):
        response = self.client.post(
            "/api/auth/register/",
            data={"email": "weak@example.com", "password": "123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("password", response.json())

    def test_registration_rejects_duplicate_email(self):
        payload = {"email": "dup@example.com", "password": "StrongPassword123!"}
        first = self.client.post("/api/auth/register/", data=payload, format="json")
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)
        second = self.client.post("/api/auth/register/", data=payload, format="json")
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", second.json())

    def test_login_is_case_and_whitespace_insensitive(self):
        self.client.post(
            "/api/auth/register/",
            data={"email": "Mixed.Case@Example.com", "password": "StrongPassword123!"},
            format="json",
        )
        for username in ("Mixed.Case@Example.com", "MIXED.CASE@EXAMPLE.COM", "  mixed.case@example.com  "):
            response = self.client.post(
                "/api/auth/token/",
                data={"username": username, "password": "StrongPassword123!"},
                format="json",
            )
            self.assertEqual(response.status_code, status.HTTP_200_OK, username)
            self.assertIn("access", response.json())

    def test_password_reset_flow(self):
        user = User.objects.create_user(
            username="reset@example.com",
            email="reset@example.com",
            password="StrongPassword123!",
        )
        # Unknown emails get the same generic response (no account enumeration).
        generic = self.client.post("/api/auth/password-reset/", data={"email": "reset@example.com"}, format="json")
        unknown = self.client.post("/api/auth/password-reset/", data={"email": "nobody@example.com"}, format="json")
        self.assertEqual(generic.status_code, status.HTTP_200_OK)
        self.assertEqual(unknown.status_code, status.HTTP_200_OK)
        self.assertEqual(generic.json(), unknown.json())

        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        confirm = self.client.post(
            "/api/auth/password-reset/confirm/",
            data={"uid": uid, "token": token, "new_password": "BrandNewPass456!"},
            format="json",
        )
        self.assertEqual(confirm.status_code, status.HTTP_200_OK)

        # Old password rejected, new password (even mixed-case email) accepted.
        old = self.client.post("/api/auth/token/", data={"username": "reset@example.com", "password": "StrongPassword123!"}, format="json")
        self.assertEqual(old.status_code, status.HTTP_401_UNAUTHORIZED)
        new = self.client.post("/api/auth/token/", data={"username": "Reset@Example.com", "password": "BrandNewPass456!"}, format="json")
        self.assertEqual(new.status_code, status.HTTP_200_OK)

        # Token is single-use: the password change invalidates it.
        reuse = self.client.post(
            "/api/auth/password-reset/confirm/",
            data={"uid": uid, "token": token, "new_password": "AnotherPass789!"},
            format="json",
        )
        self.assertEqual(reuse.status_code, status.HTTP_400_BAD_REQUEST)

    def test_password_reset_confirm_rejects_weak_password(self):
        user = User.objects.create_user(
            username="resetweak@example.com",
            email="resetweak@example.com",
            password="StrongPassword123!",
        )
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        token = default_token_generator.make_token(user)
        response = self.client.post(
            "/api/auth/password-reset/confirm/",
            data={"uid": uid, "token": token, "new_password": "123"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("new_password", response.json())

    def test_email_verification_flow(self):
        user = User.objects.create_user(
            username="verify@example.com",
            email="verify@example.com",
            password="StrongPassword123!",
        )
        self.assertFalse(user.is_verified)

        bad = self.client.get("/api/auth/verify/", {"token": "not-a-real-token"})
        self.assertEqual(bad.status_code, status.HTTP_400_BAD_REQUEST)

        token = make_verification_token(user)
        ok = self.client.get("/api/auth/verify/", {"token": token})
        self.assertEqual(ok.status_code, status.HTTP_200_OK)
        user.refresh_from_db()
        self.assertTrue(user.is_verified)


class SupportStaffPermissionTests(TestCase):
    def setUp(self):
        self.permission = IsSupportStaff()
        self.client_user = User.objects.create_user(
            username="client@example.com",
            email="client@example.com",
            password="Password123!",
            role=User.Role.CLIENT,
        )
        self.admin_user = User.objects.create_user(
            username="admin@example.com",
            email="admin@example.com",
            password="Password123!",
            role=User.Role.ADMIN,
        )
        self.staff_user = User.objects.create_user(
            username="staff@example.com",
            email="staff@example.com",
            password="Password123!",
            is_staff=True,
        )

    def test_client_is_denied(self):
        class DummyRequest:
            user = self.client_user

        self.assertFalse(self.permission.has_permission(DummyRequest(), None))

    def test_admin_role_is_allowed(self):
        class DummyRequest:
            user = self.admin_user

        self.assertTrue(self.permission.has_permission(DummyRequest(), None))

    def test_staff_flag_is_allowed(self):
        class DummyRequest:
            user = self.staff_user

        self.assertTrue(self.permission.has_permission(DummyRequest(), None))


class SupportConversationAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.client_user = User.objects.create_user(
            username="client2@example.com",
            email="client2@example.com",
            password="Password123!",
            role=User.Role.CLIENT,
        )
        self.staff_user = User.objects.create_user(
            username="support@example.com",
            email="support@example.com",
            password="Password123!",
            is_staff=True,
            role=User.Role.ADMIN,
        )

    def test_create_conversation_and_messages(self):
        self.client.force_authenticate(user=self.client_user)
        payload = {
            "subject": "Deposit delay",
            "initial_message": "My demo deposit is taking long.",
        }
        response = self.client.post("/api/conversations/", data=payload, format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        conv_id = response.json()["id"]

        # Client can fetch own conversations
        list_response = self.client.get("/api/conversations/")
        self.assertEqual(list_response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list_response.json()), 1)

        # Staff can assign the conversation
        self.client.force_authenticate(user=self.staff_user)
        assign_response = self.client.post(
            f"/api/conversations/{conv_id}/assign/",
            data={"assigned_to": self.staff_user.id},
            format="json",
        )
        self.assertEqual(assign_response.status_code, status.HTTP_200_OK)

        # Non-staff cannot assign
        self.client.force_authenticate(user=self.client_user)
        assign_denied = self.client.post(
            f"/api/conversations/{conv_id}/assign/",
            data={"assigned_to": self.staff_user.id},
            format="json",
        )
        self.assertEqual(assign_denied.status_code, status.HTTP_403_FORBIDDEN)


class WatchlistAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="watch@example.com",
            email="watch@example.com",
            password="Password123!",
        )
        self.client.force_authenticate(user=self.user)

    def test_watchlist_add_list_remove(self):
        # Add
        add = self.client.post("/api/watchlist/", data={"symbol": "AAPL"}, format="json")
        self.assertEqual(add.status_code, status.HTTP_201_CREATED)
        self.assertEqual(add.json()["symbol"], "AAPL")

        # Idempotent add does not duplicate
        self.client.post("/api/watchlist/", data={"symbol": "AAPL"}, format="json")
        listing = self.client.get("/api/watchlist/")
        self.assertEqual(listing.status_code, status.HTTP_200_OK)
        self.assertEqual(len(listing.json()), 1)

        # Remove
        remove = self.client.delete("/api/watchlist/AAPL/")
        self.assertEqual(remove.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(len(self.client.get("/api/watchlist/").json()), 0)

    def test_watchlist_requires_authentication(self):
        anon = APIClient()
        res = anon.get("/api/watchlist/")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)


@override_settings(FUNDING_DEMO_TOPUP_ENABLED=True)
class DemoTradingAPITests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="trader2@example.com",
            email="trader2@example.com",
            password="Password123!",
        )
        DemoAccount.objects.create(user=self.user, currency="USD", cash_balance=Decimal("10000.00"))
        self.client.force_authenticate(user=self.user)

    def test_topup_credits_demo_cash_and_records_transaction(self):
        res = self.client.post("/api/portfolio/topup/", data={"amount": "500.00"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        body = res.json()
        self.assertTrue(body["demo"])
        self.assertEqual(body["account"]["cash_balance"], "10500.00")
        self.assertEqual(Transaction.objects.filter(user=self.user, side="topup").count(), 1)

    def test_topup_rejects_non_positive(self):
        res = self.client.post("/api/portfolio/topup/", data={"amount": "-5"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_buy_then_sell_round_trip(self):
        # AAPL is seeded by the market snapshot fallback with a positive price.
        buy = self.client.post("/api/orders/", data={"symbol": "AAPL", "side": "buy", "quantity": "2"}, format="json")
        self.assertEqual(buy.status_code, status.HTTP_201_CREATED, buy.content)
        self.assertTrue(buy.json()["demo"])
        self.assertEqual(Position.objects.filter(user=self.user).count(), 1)

        sell = self.client.post("/api/orders/", data={"symbol": "AAPL", "side": "sell", "quantity": "2"}, format="json")
        self.assertEqual(sell.status_code, status.HTTP_201_CREATED, sell.content)
        self.assertEqual(Position.objects.filter(user=self.user).count(), 0)
        self.assertEqual(Transaction.objects.filter(user=self.user, side__in=["buy", "sell"]).count(), 2)

    def test_sell_without_position_rejected(self):
        res = self.client.post("/api/orders/", data={"symbol": "AAPL", "side": "sell", "quantity": "1"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_side_rejected(self):
        res = self.client.post("/api/orders/", data={"symbol": "AAPL", "side": "short", "quantity": "1"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


class DemoTopUpFundingToggleTests(TestCase):
    """FUNDING_DEMO_TOPUP_ENABLED gates the simulated top-up endpoint.

    The setting defaults to False in every environment, so the endpoint must
    refuse to mint demo funds unless it is explicitly enabled (development or
    tests). These tests pin the default, the disabled behaviour (403 + no
    mutation) and the enabled behaviour.
    """

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="toggle@example.com",
            email="toggle@example.com",
            password="Password123!",
        )
        DemoAccount.objects.create(user=self.user, currency="USD", cash_balance=Decimal("100.00"))
        self.url = "/api/portfolio/topup/"

    def test_setting_defaults_to_false(self):
        self.assertFalse(settings.FUNDING_DEMO_TOPUP_ENABLED)

    def test_default_disabled_returns_403_without_mutation(self):
        # No override: the real default (False) must block the endpoint.
        self.client.force_authenticate(user=self.user)
        res = self.client.post(self.url, data={"amount": "500.00"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.user.demo_account.refresh_from_db()
        self.assertEqual(self.user.demo_account.cash_balance, Decimal("100.00"))
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 0)

    @override_settings(FUNDING_DEMO_TOPUP_ENABLED=False)
    def test_explicitly_disabled_returns_403_without_mutation(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.post(self.url, data={"amount": "500.00"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)
        self.user.demo_account.refresh_from_db()
        self.assertEqual(self.user.demo_account.cash_balance, Decimal("100.00"))
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 0)

    @override_settings(FUNDING_DEMO_TOPUP_ENABLED=True)
    def test_enabled_credits_demo_cash(self):
        self.client.force_authenticate(user=self.user)
        res = self.client.post(self.url, data={"amount": "500.00"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertTrue(res.json()["demo"])
        self.user.demo_account.refresh_from_db()
        self.assertEqual(self.user.demo_account.cash_balance, Decimal("600.00"))
        self.assertEqual(Transaction.objects.filter(user=self.user, side="topup").count(), 1)

    @override_settings(FUNDING_DEMO_TOPUP_ENABLED=True)
    def test_unauthenticated_is_still_protected(self):
        # Even with demo funding enabled, the endpoint stays behind auth.
        res = self.client.post(self.url, data={"amount": "500.00"}, format="json")
        self.assertIn(res.status_code, (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertEqual(Transaction.objects.filter(user=self.user).count(), 0)
        self.user.demo_account.refresh_from_db()
        self.assertEqual(self.user.demo_account.cash_balance, Decimal("100.00"))


class DemoFundingProductionGuardTests(TestCase):
    """A production deployment must never be able to enable simulated funding.

    The safety valve runs at settings load time, so these tests load a fresh
    copy of the settings module in a subprocess with a controlled environment.
    """

    def _load_funding_setting(self, env_overrides):
        # Import the settings module directly rather than calling django.setup():
        # setup() would try to load the database backend, which is unnecessary
        # here and unavailable in some environments (e.g. no psycopg2).
        code = (
            "import veltrix_project.settings as s;"
            "print(s.FUNDING_DEMO_TOPUP_ENABLED)"
        )
        env = dict(os.environ)
        # Deterministic baseline for the scenario under test.
        env.pop("FUNDING_DEMO_TOPUP_ENABLED", None)
        env.pop("DATABASE_URL", None)
        env["DJANGO_SETTINGS_MODULE"] = "veltrix_project.settings"
        env["DEBUG"] = "1"
        env["SECRET_KEY"] = "test-only-secret"
        env.update(env_overrides)
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(settings.BASE_DIR),
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()

    def test_production_debug_off_forces_funding_off(self):
        self.assertEqual(
            self._load_funding_setting({"DEBUG": "0", "FUNDING_DEMO_TOPUP_ENABLED": "1"}),
            "False",
        )

    def test_production_with_database_url_forces_funding_off_even_if_debug_defaults(self):
        self.assertEqual(
            self._load_funding_setting(
                {"DATABASE_URL": "postgres://user:pass@localhost/veltrix", "FUNDING_DEMO_TOPUP_ENABLED": "1"}
            ),
            "False",
        )

    def test_local_dev_can_explicitly_enable_funding(self):
        self.assertEqual(
            self._load_funding_setting({"DEBUG": "1", "FUNDING_DEMO_TOPUP_ENABLED": "1"}),
            "True",
        )


class ConversationMessageContractTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="contract@example.com",
            email="contract@example.com",
            password="Password123!",
        )
        self.client.force_authenticate(user=self.user)

    def test_list_serializer_includes_messages_and_staff_flag(self):
        self.client.post(
            "/api/conversations/",
            data={"subject": "Need help", "initial_message": "Hello desk"},
            format="json",
        )
        res = self.client.get("/api/conversations/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        convo = res.json()[0]
        # Frontend depends on conversation.messages for the thread view
        self.assertIn("messages", convo)
        self.assertEqual(len(convo["messages"]), 1)
        # sender is a nested object so the UI can distinguish self vs staff
        self.assertIn("is_staff", convo["messages"][0]["sender"])
        self.assertEqual(convo["messages"][0]["sender"]["id"], self.user.id)
        # A flat sender_id is provided so the UI can attribute a message
        # without parsing the nested object or trusting content prefixes.
        self.assertEqual(convo["messages"][0]["sender_id"], self.user.id)


class AdminMessagingFlowTests(TestCase):
    """End-to-end contract for admin <-> client support messaging.

    These tests pin the behaviour that had no regression coverage: a client
    message produces a durable admin-visible unread notification, an
    authorized admin can reply to the correct conversation, the reply is
    attributed to the admin and is visible to the owning client only, and no
    other client can read or write the conversation.
    """

    def setUp(self):
        self.client_user = User.objects.create_user(
            username="owner@example.com",
            email="owner@example.com",
            password="Password123!",
            role=User.Role.CLIENT,
        )
        self.other_client = User.objects.create_user(
            username="intruder@example.com",
            email="intruder@example.com",
            password="Password123!",
            role=User.Role.CLIENT,
        )
        self.admin = User.objects.create_user(
            username="staff@example.com",
            email="staff@example.com",
            password="Password123!",
            role=User.Role.ADMIN,
            is_staff=True,
        )
        self.conv = Conversation.objects.create(user=self.client_user, subject="Deposit query")

    # ---- Issue 1: admin notification on a new client message ----

    def test_client_message_is_persisted_and_notifies_admin(self):
        api = APIClient()
        api.force_authenticate(user=self.client_user)
        res = api.post(
            f"/api/conversations/{self.conv.id}/messages/",
            data={"content": "Please help me understand my balance."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        # Persisted exactly once.
        self.assertEqual(Message.objects.filter(conversation=self.conv).count(), 1)

        # The admin is notified through the existing unread counter, which is
        # what the admin support inbox reads on each poll.
        admin_api = APIClient()
        admin_api.force_authenticate(user=self.admin)
        listing = admin_api.get("/api/conversations/").json()
        convo = next(c for c in listing if c["id"] == self.conv.id)
        self.assertEqual(convo["unread_count"], 1)

    def test_admin_unread_count_does_not_double_count_on_refetch(self):
        Message.objects.create(conversation=self.conv, sender=self.client_user, content="ping")
        admin_api = APIClient()
        admin_api.force_authenticate(user=self.admin)
        # Repeated polls (the frontend does this) must be stable, not additive.
        for _ in range(3):
            listing = admin_api.get("/api/conversations/").json()
            self.assertEqual(listing[0]["unread_count"], 1)

    def test_mark_read_is_idempotent_for_admin(self):
        Message.objects.create(conversation=self.conv, sender=self.client_user, content="ping")
        admin_api = APIClient()
        admin_api.force_authenticate(user=self.admin)
        first = admin_api.post(f"/api/conversations/{self.conv.id}/mark-read/").json()
        second = admin_api.post(f"/api/conversations/{self.conv.id}/mark-read/").json()
        self.assertEqual(first, {"marked": 1})
        self.assertEqual(second, {"marked": 0})
        listing = admin_api.get("/api/conversations/").json()
        self.assertEqual(listing[0]["unread_count"], 0)

    # ---- Issue 2: admin replies reach the correct client ----

    def test_admin_reply_is_persisted_and_visible_to_owner(self):
        Message.objects.create(conversation=self.conv, sender=self.client_user, content="Hello")

        admin_api = APIClient()
        admin_api.force_authenticate(user=self.admin)
        res = admin_api.post(
            f"/api/conversations/{self.conv.id}/messages/",
            data={"content": "[STAFF RESPONSE] Your balance is a simulated demo balance."},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        body = res.json()
        # Server identifies the admin as the sender (both nested + flat id).
        self.assertEqual(body["sender"]["id"], self.admin.id)
        self.assertEqual(body["sender_id"], self.admin.id)
        self.assertTrue(body["sender"]["is_staff"])
        self.assertFalse(body["is_read"])

        # The reply is persisted with the admin as sender.
        reply = Message.objects.get(id=body["id"])
        self.assertEqual(reply.sender_id, self.admin.id)
        self.assertEqual(reply.conversation_id, self.conv.id)

        # The owning client can fetch and see it in the correct conversation.
        client_api = APIClient()
        client_api.force_authenticate(user=self.client_user)
        messages = client_api.get(f"/api/conversations/{self.conv.id}/messages/").json()["results"]
        self.assertTrue(any(m["id"] == body["id"] for m in messages))

        # The client is notified of the staff reply via the unread counter.
        listing = client_api.get("/api/conversations/").json()
        self.assertEqual(listing[0]["unread_count"], 1)

    def test_other_clients_cannot_read_or_write_the_conversation(self):
        Message.objects.create(conversation=self.conv, sender=self.client_user, content="private")

        intruder_api = APIClient()
        intruder_api.force_authenticate(user=self.other_client)
        self.assertEqual(
            intruder_api.get(f"/api/conversations/{self.conv.id}/messages/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            intruder_api.get(f"/api/conversations/{self.conv.id}/").status_code,
            status.HTTP_404_NOT_FOUND,
        )
        self.assertEqual(
            intruder_api.post(
                f"/api/conversations/{self.conv.id}/messages/",
                data={"content": "hijack"},
                format="json",
            ).status_code,
            status.HTTP_404_NOT_FOUND,
        )
        # No message was written by the intruder.
        self.assertFalse(Message.objects.filter(sender=self.other_client).exists())

        # The other client's own list never exposes the conversation.
        own = intruder_api.get("/api/conversations/").json()
        self.assertFalse(any(c["id"] == self.conv.id for c in own))

    def test_admin_reply_to_closed_conversation_is_rejected(self):
        self.conv.is_active = False
        self.conv.save(update_fields=["is_active"])
        admin_api = APIClient()
        admin_api.force_authenticate(user=self.admin)
        res = admin_api.post(
            f"/api/conversations/{self.conv.id}/messages/",
            data={"content": "too late"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Message.objects.filter(conversation=self.conv).count(), 0)

    def test_blank_admin_reply_is_rejected_without_persisting(self):
        admin_api = APIClient()
        admin_api.force_authenticate(user=self.admin)
        res = admin_api.post(
            f"/api/conversations/{self.conv.id}/messages/",
            data={"content": "   "},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(Message.objects.filter(conversation=self.conv).count(), 0)

    def test_ordinary_client_cannot_reply_as_staff(self):
        # A client posting to their own conversation is allowed, but the server
        # always records *that client* as sender — never a staff identity — so
        # a client can never forge an admin reply.
        client_api = APIClient()
        client_api.force_authenticate(user=self.client_user)
        res = client_api.post(
            f"/api/conversations/{self.conv.id}/messages/",
            data={"content": "[STAFF RESPONSE] fake"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.json()["sender"]["id"], self.client_user.id)
        self.assertFalse(res.json()["sender"]["is_staff"])

    def test_client_message_updates_conversation_and_not_other_conversations(self):
        other_conv = Conversation.objects.create(user=self.client_user, subject="Second")
        admin_api = APIClient()
        admin_api.force_authenticate(user=self.admin)

        client_api = APIClient()
        client_api.force_authenticate(user=self.client_user)
        client_api.post(
            f"/api/conversations/{self.conv.id}/messages/",
            data={"content": "only this thread"},
            format="json",
        )

        listing = admin_api.get("/api/conversations/").json()
        by_id = {c["id"]: c for c in listing}
        self.assertEqual(by_id[self.conv.id]["unread_count"], 1)
        self.assertEqual(by_id[other_conv.id]["unread_count"], 0)


class EnsureSuperuserCommandTests(TestCase):
    """The confirmed admin-reset command used by the create-django-admin workflow."""

    PASSWORD = "AdminPass123!"
    USERNAME = "veltrixadmin"
    EMAIL = "veltrix@gmail.com"
    CONFIRM = "RESET_EXISTING_ADMIN"

    def _env(self, **overrides):
        env = {
            "DJANGO_SUPERUSER_USERNAME": self.USERNAME,
            "DJANGO_SUPERUSER_EMAIL": self.EMAIL,
            "DJANGO_SUPERUSER_PASSWORD": self.PASSWORD,
        }
        env.update(overrides)
        return env

    def _create_active_superuser(
        self, username="oldadmin@example.com", password="OldPassword123!", **kwargs
    ):
        defaults = {
            "email": username,
            "password": password,
            "role": User.Role.ADMIN,
            "is_staff": True,
            "is_superuser": True,
            "is_verified": True,
            "is_active": True,
        }
        defaults.update(kwargs)
        return User.objects.create_user(username=username, **defaults)

    def _call(self, confirm=CONFIRM, **kwargs):
        call_command("ensure_superuser", confirm=confirm, **kwargs)

    def test_incorrect_confirmation_is_rejected_before_any_change(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call(confirm="yes")
        self.assertIn("RESET_EXISTING_ADMIN", str(ctx.exception))

        user = User.objects.get(username="oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))
        self.assertEqual(User.objects.count(), 1)

    def test_missing_confirmation_is_rejected(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError):
                self._call(confirm="")
        self.assertEqual(User.objects.get().username, "oldadmin@example.com")

    def test_zero_existing_superusers_fails_without_creating_anything(self):
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("exactly one active superuser", str(ctx.exception))
        self.assertEqual(User.objects.count(), 0)

    def test_multiple_superusers_fail_without_changes(self):
        first = self._create_active_superuser(username="admin1@example.com")
        second = self._create_active_superuser(username="admin2@example.com")
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("exactly one active superuser", str(ctx.exception))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(first.username, "admin1@example.com")
        self.assertEqual(second.username, "admin2@example.com")
        self.assertTrue(first.check_password("OldPassword123!"))
        self.assertTrue(second.check_password("OldPassword123!"))

    def test_inactive_superuser_is_not_reset(self):
        self._create_active_superuser(is_active=False)
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError):
                self._call()
        user = User.objects.get()
        self.assertFalse(user.is_active)
        self.assertEqual(user.username, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_username_conflict_with_another_account_aborts(self):
        target = self._create_active_superuser()
        User.objects.create_user(
            username=self.USERNAME,
            email="collision@example.com",
            password="OtherUserPass123!",
        )
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("already held by another account", str(ctx.exception))

        target.refresh_from_db()
        self.assertEqual(target.username, "oldadmin@example.com")
        self.assertEqual(target.email, "oldadmin@example.com")
        self.assertTrue(target.check_password("OldPassword123!"))
        self.assertEqual(User.objects.count(), 2)

    def test_email_conflict_with_another_account_aborts(self):
        target = self._create_active_superuser()
        User.objects.create_user(
            username="someone-else@example.com",
            email=self.EMAIL,
            password="OtherUserPass123!",
        )
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("already held by another account", str(ctx.exception))

        target.refresh_from_db()
        self.assertEqual(target.username, "oldadmin@example.com")
        self.assertTrue(target.check_password("OldPassword123!"))

    def test_missing_password_fails_before_write(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            os.environ.pop("DJANGO_SUPERUSER_PASSWORD", None)
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("DJANGO_SUPERUSER_PASSWORD", str(ctx.exception))

        user = User.objects.get()
        self.assertEqual(user.username, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_weak_password_fails_before_write(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env(DJANGO_SUPERUSER_PASSWORD="123")):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("password validators", str(ctx.exception))

        user = User.objects.get()
        self.assertEqual(user.username, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_invalid_email_rejected_before_write(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env(DJANGO_SUPERUSER_EMAIL="not-an-email")):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("email validators", str(ctx.exception))

        user = User.objects.get()
        self.assertEqual(user.email, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_successful_reset_updates_the_single_existing_account(self):
        target = self._create_active_superuser(is_verified=False)
        env = self._env(
            DJANGO_SUPERUSER_USERNAME="  VeltrixAdmin  ",
            DJANGO_SUPERUSER_EMAIL="Veltrix@Gmail.com",
        )
        with mock.patch.dict(os.environ, env):
            out = StringIO()
            self._call(stdout=out)

        target.refresh_from_db()
        self.assertEqual(target.username, self.USERNAME)
        self.assertEqual(target.email, self.EMAIL)
        self.assertTrue(target.check_password(self.PASSWORD))
        self.assertTrue(target.is_active)
        self.assertTrue(target.is_staff)
        self.assertTrue(target.is_superuser)
        self.assertEqual(target.role, User.Role.ADMIN)
        self.assertTrue(target.is_verified)
        self.assertIn(self.USERNAME, out.getvalue())
        self.assertIn(self.EMAIL, out.getvalue())

    def test_identity_values_are_read_from_environment_variables(self):
        target = self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            self._call()

        target.refresh_from_db()
        self.assertEqual(target.username, self.USERNAME)
        self.assertEqual(target.email, self.EMAIL)

    def test_reset_never_creates_a_duplicate_account(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            self._call()
            self._call()

        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get()
        self.assertEqual(user.username, self.USERNAME)
        self.assertEqual(user.email, self.EMAIL)
        self.assertTrue(user.check_password(self.PASSWORD))

    def test_other_accounts_are_never_modified(self):
        target = self._create_active_superuser()
        bystander = User.objects.create_user(
            username="bystander@example.com",
            email="bystander@example.com",
            password="BystanderPass123!",
        )
        with mock.patch.dict(os.environ, self._env()):
            self._call()

        target.refresh_from_db()
        bystander.refresh_from_db()
        self.assertEqual(target.username, self.USERNAME)
        self.assertEqual(bystander.username, "bystander@example.com")
        self.assertEqual(bystander.role, User.Role.CLIENT)
        self.assertFalse(bystander.is_staff)
        self.assertFalse(bystander.is_superuser)
        self.assertTrue(bystander.check_password("BystanderPass123!"))
        self.assertEqual(User.objects.count(), 2)

    def test_failure_during_save_rolls_back_all_changes(self):
        target = self._create_active_superuser(is_verified=False)
        with mock.patch.dict(os.environ, self._env()):
            with mock.patch.object(User, "save", side_effect=RuntimeError("simulated failure")):
                with self.assertRaises(RuntimeError):
                    self._call()

        target.refresh_from_db()
        self.assertEqual(User.objects.count(), 1)
        self.assertEqual(target.username, "oldadmin@example.com")
        self.assertEqual(target.email, "oldadmin@example.com")
        self.assertTrue(target.check_password("OldPassword123!"))
        self.assertEqual(target.role, User.Role.ADMIN)
        # The in-transaction promotion to is_verified=True was rolled back too.
        self.assertFalse(target.is_verified)

    def test_password_is_never_printed(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            out = StringIO()
            self._call(stdout=out)
        self.assertNotIn(self.PASSWORD, out.getvalue())


class RecoverSuperuserCommandTests(TestCase):
    """The manual recovery command used by the recover-django-admin workflow."""

    PASSWORD = "RecoveryPass123!"
    USERNAME = "veltrixadmin"
    EMAIL = "veltrix@gmail.com"

    def _env(self, **overrides):
        env = {
            "DJANGO_ADMIN_RECOVERY_USERNAME": self.USERNAME,
            "DJANGO_ADMIN_RECOVERY_EMAIL": self.EMAIL,
            "DJANGO_ADMIN_RECOVERY_PASSWORD": self.PASSWORD,
        }
        env.update(overrides)
        return env

    def _create_active_superuser(
        self, username="oldadmin@example.com", password="OldPassword123!", **kwargs
    ):
        defaults = {
            "email": username,
            "password": password,
            "role": User.Role.ADMIN,
            "is_staff": True,
            "is_superuser": True,
            "is_verified": True,
            "is_active": True,
        }
        defaults.update(kwargs)
        return User.objects.create_user(username=username, **defaults)

    def _call(self, confirm="RESET_EXISTING_ADMIN", **kwargs):
        call_command("recover_superuser", confirm=confirm, **kwargs)

    def test_incorrect_confirmation_is_rejected_before_any_change(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call(confirm="yes")
        self.assertIn("RESET_EXISTING_ADMIN", str(ctx.exception))

        user = User.objects.get(username="oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))
        self.assertEqual(User.objects.count(), 1)

    def test_missing_confirmation_is_rejected(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError):
                self._call(confirm="")
        self.assertEqual(User.objects.get().username, "oldadmin@example.com")

    def test_zero_active_superusers_fails_without_creating_anything(self):
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("exactly one active superuser", str(ctx.exception))
        self.assertEqual(User.objects.count(), 0)

    def test_multiple_active_superusers_fail_without_changes(self):
        first = self._create_active_superuser(username="admin1@example.com")
        second = self._create_active_superuser(username="admin2@example.com")
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("exactly one active superuser", str(ctx.exception))

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(first.username, "admin1@example.com")
        self.assertEqual(second.username, "admin2@example.com")
        self.assertTrue(first.check_password("OldPassword123!"))
        self.assertTrue(second.check_password("OldPassword123!"))

    def test_inactive_superuser_is_not_recovered(self):
        self._create_active_superuser(is_active=False)
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError):
                self._call()
        user = User.objects.get()
        self.assertFalse(user.is_active)
        self.assertEqual(user.username, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_username_conflict_with_another_account_aborts(self):
        target = self._create_active_superuser()
        User.objects.create_user(
            username=self.USERNAME,
            email="collision@example.com",
            password="OtherUserPass123!",
        )
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("already held by another account", str(ctx.exception))

        target.refresh_from_db()
        self.assertEqual(target.username, "oldadmin@example.com")
        self.assertEqual(target.email, "oldadmin@example.com")
        self.assertTrue(target.check_password("OldPassword123!"))
        self.assertEqual(User.objects.count(), 2)

    def test_email_conflict_with_another_account_aborts(self):
        target = self._create_active_superuser()
        User.objects.create_user(
            username="someone-else@example.com",
            email=self.EMAIL,
            password="OtherUserPass123!",
        )
        with mock.patch.dict(os.environ, self._env()):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("already held by another account", str(ctx.exception))

        target.refresh_from_db()
        self.assertEqual(target.username, "oldadmin@example.com")
        self.assertTrue(target.check_password("OldPassword123!"))

    def test_missing_recovery_password_fails_before_write(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            os.environ.pop("DJANGO_ADMIN_RECOVERY_PASSWORD", None)
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("DJANGO_ADMIN_RECOVERY_PASSWORD", str(ctx.exception))

        user = User.objects.get()
        self.assertEqual(user.username, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_weak_recovery_password_fails_before_write(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env(DJANGO_ADMIN_RECOVERY_PASSWORD="123")):
            with self.assertRaises(CommandError) as ctx:
                self._call()
        self.assertIn("password validators", str(ctx.exception))

        user = User.objects.get()
        self.assertEqual(user.username, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_invalid_email_rejected_before_write(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env(DJANGO_ADMIN_RECOVERY_EMAIL="not-an-email")):
            with self.assertRaises(CommandError):
                self._call()
        user = User.objects.get()
        self.assertEqual(user.email, "oldadmin@example.com")
        self.assertTrue(user.check_password("OldPassword123!"))

    def test_successful_recovery_updates_the_single_existing_admin(self):
        target = self._create_active_superuser(is_verified=False)
        env = self._env(
            DJANGO_ADMIN_RECOVERY_USERNAME="  VeltrixAdmin  ",
            DJANGO_ADMIN_RECOVERY_EMAIL="Veltrix@Gmail.com",
        )
        with mock.patch.dict(os.environ, env):
            out = StringIO()
            self._call(stdout=out)

        target.refresh_from_db()
        self.assertEqual(target.username, self.USERNAME)
        self.assertEqual(target.email, self.EMAIL)
        self.assertTrue(target.check_password(self.PASSWORD))
        self.assertTrue(target.is_active)
        self.assertTrue(target.is_staff)
        self.assertTrue(target.is_superuser)
        self.assertEqual(target.role, User.Role.ADMIN)
        self.assertTrue(target.is_verified)
        self.assertIn(self.USERNAME, out.getvalue())
        self.assertIn(self.EMAIL, out.getvalue())

    def test_recovery_does_not_create_duplicate_users(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            self._call()
            self._call()

        self.assertEqual(User.objects.count(), 1)
        user = User.objects.get()
        self.assertEqual(user.username, self.USERNAME)
        self.assertTrue(user.check_password(self.PASSWORD))

    def test_other_accounts_are_never_modified(self):
        target = self._create_active_superuser()
        bystander = User.objects.create_user(
            username="bystander@example.com",
            email="bystander@example.com",
            password="BystanderPass123!",
        )
        with mock.patch.dict(os.environ, self._env()):
            self._call()

        target.refresh_from_db()
        bystander.refresh_from_db()
        self.assertEqual(target.username, self.USERNAME)
        self.assertEqual(bystander.username, "bystander@example.com")
        self.assertEqual(bystander.role, User.Role.CLIENT)
        self.assertFalse(bystander.is_staff)
        self.assertFalse(bystander.is_superuser)
        self.assertTrue(bystander.check_password("BystanderPass123!"))
        self.assertEqual(User.objects.count(), 2)

    def test_password_is_never_printed(self):
        self._create_active_superuser()
        with mock.patch.dict(os.environ, self._env()):
            out = StringIO()
            self._call(stdout=out)
        self.assertNotIn(self.PASSWORD, out.getvalue())


class ProfileUpdateAPITests(TestCase):
    """Dashboard profile editing: only safe fields are writable."""

    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="profile@example.com",
            email="profile@example.com",
            password="Password123!",
            first_name="Plain",
            last_name="User",
        )
        self.other = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="Password123!",
        )
        self.admin = User.objects.create_superuser(
            username="boss@example.com",
            email="boss@example.com",
            password="Password123!",
        )
        # create_superuser does not set the custom role/verified fields; mirror
        # the permanent admin account's shape so privilege-preservation tests
        # are realistic.
        self.admin.role = User.Role.ADMIN
        self.admin.is_verified = True
        self.admin.save()

    def test_get_profile(self):
        self.client.force_authenticate(self.user)
        res = self.client.get("/api/profile/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.json()["email"], "profile@example.com")
        self.assertEqual(res.json()["first_name"], "Plain")

    def test_user_can_update_own_name_and_email(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(
            "/api/profile/",
            data={"first_name": "Updated", "last_name": "Name", "email": "New.Email@Example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Updated")
        self.assertEqual(self.user.last_name, "Name")
        self.assertEqual(self.user.email, "new.email@example.com")
        # Login treats username as the lowercased email, so they stay in sync.
        self.assertEqual(self.user.username, "new.email@example.com")
        self.assertEqual(res.json()["username"], "new.email@example.com")

    def test_privileged_fields_cannot_be_escalated_via_patch(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(
            "/api/profile/",
            data={
                "first_name": "Sneaky",
                "is_staff": True,
                "role": User.Role.ADMIN,
                "is_verified": True,
                "is_superuser": True,
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Sneaky")  # permitted field applied
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.user.role, User.Role.CLIENT)
        self.assertFalse(self.user.is_verified)

        body = res.json()
        self.assertFalse(body["is_staff"])
        self.assertEqual(body["role"], User.Role.CLIENT)
        self.assertFalse(body["is_verified"])

    def test_privileged_fields_cannot_be_escalated_via_put(self):
        self.client.force_authenticate(self.user)
        res = self.client.put(
            "/api/profile/",
            data={"first_name": "Sneaky", "is_staff": True, "role": "admin", "is_superuser": True},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

        self.user.refresh_from_db()
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertEqual(self.user.role, User.Role.CLIENT)

    def test_username_cannot_be_changed(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(
            "/api/profile/",
            data={"username": "hijack@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.user.refresh_from_db()
        self.assertEqual(self.user.username, "profile@example.com")

    def test_email_uniqueness_enforced(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(
            "/api/profile/",
            data={"email": "Other@Example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", res.json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "profile@example.com")

    def test_admin_profile_update_preserves_privileges(self):
        self.client.force_authenticate(self.admin)
        res = self.client.patch(
            "/api/profile/",
            data={"first_name": "Chief", "email": "boss.new@example.com"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

        self.admin.refresh_from_db()
        self.assertEqual(self.admin.first_name, "Chief")
        self.assertEqual(self.admin.email, "boss.new@example.com")
        self.assertEqual(self.admin.username, "boss.new@example.com")
        self.assertTrue(self.admin.is_staff)
        self.assertTrue(self.admin.is_superuser)
        self.assertEqual(self.admin.role, User.Role.ADMIN)
        self.assertTrue(self.admin.is_verified)

    def test_profile_update_requires_authentication(self):
        res = self.client.patch("/api/profile/", data={"first_name": "Anon"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)
