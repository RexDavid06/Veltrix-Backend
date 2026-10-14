from decimal import Decimal
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from veltrix_app.api.permissions import IsSupportStaff
from veltrix_app.models import Conversation, DemoAccount, Instrument, Message

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
        self.assertEqual(account.cash_balance, Decimal("10000.00"))

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
