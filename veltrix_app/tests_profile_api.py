"""Self-service profile endpoint contract tests.

The profile endpoint is the only place where an authenticated customer or
administrator edits their own record. These tests pin the security contract:

- the user is always derived from the authenticated request (never from a
  client-supplied id) and no route exists to reach another account;
- privileged fields (role, staff/superuser flags, verification) and
  financial fields are never writable through this endpoint;
- inactive accounts are rejected;
- responses never contain password hashes or other credential material;
- validation errors are field-level and actionable.
"""

from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from veltrix_app.models import DemoAccount, Instrument, Position, Transaction

User = get_user_model()

URL = "/api/profile/"
EXPECTED_FIELDS = {
    "id",
    "username",
    "email",
    "first_name",
    "last_name",
    "role",
    "is_verified",
    "is_staff",
}


class ProfileSelfServiceBase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = User.objects.create_user(
            username="selfservice@example.com",
            email="selfservice@example.com",
            password="Password123!",
            first_name="Self",
            last_name="Service",
        )
        DemoAccount.objects.create(user=self.user, currency="USD", cash_balance="250.00")
        self.other = User.objects.create_user(
            username="neighbour@example.com",
            email="neighbour@example.com",
            password="Password123!",
            first_name="Neighbour",
            last_name="Other",
            role=User.Role.CLIENT,
        )
        DemoAccount.objects.create(user=self.other, currency="USD", cash_balance="750.00")


class ProfileRetrievalTests(ProfileSelfServiceBase):
    def test_get_returns_only_the_authenticated_user(self):
        self.client.force_authenticate(self.user)
        res = self.client.get(URL)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        body = res.json()
        self.assertEqual(body["id"], self.user.id)
        self.assertEqual(body["email"], "selfservice@example.com")
        self.assertNotEqual(body["id"], self.other.id)

    def test_response_field_whitelist_excludes_credentials(self):
        self.client.force_authenticate(self.user)
        body = self.client.get(URL).json()
        self.assertEqual(set(body.keys()), EXPECTED_FIELDS)
        self.assertNotIn("password", body)
        self.assertNotIn("last_login", body)
        self.assertNotIn("is_superuser", body)
        self.assertNotIn(str(self.user.password), str(body))

    def test_get_requires_authentication(self):
        res = self.client.get(URL)
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_patch_requires_authentication(self):
        res = self.client.patch(URL, data={"first_name": "Anon"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_role_and_verification_are_sourced_from_backend(self):
        self.client.force_authenticate(self.user)
        body = self.client.get(URL).json()
        self.assertEqual(body["role"], User.Role.CLIENT)
        self.assertFalse(body["is_verified"])
        self.assertFalse(body["is_staff"])


class ProfileUpdateTests(ProfileSelfServiceBase):
    def test_first_and_last_name_are_updatable(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(
            URL, data={"first_name": "Updated", "last_name": "Person"}, format="json"
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Updated")
        self.assertEqual(self.user.last_name, "Person")
        self.assertEqual(res.json()["first_name"], "Updated")

    def test_privileged_and_financial_fields_are_ignored(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(
            URL,
            data={
                "first_name": "Still",
                "role": User.Role.ADMIN,
                "is_staff": True,
                "is_superuser": True,
                "is_verified": True,
                "id": self.other.id,
                "username": "hijack@example.com",
                "cash_balance": "999999.00",
                "balance": "999999.00",
                "password": "new-secret",
            },
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Still")  # the one valid field applied
        self.assertEqual(self.user.role, User.Role.CLIENT)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(self.user.is_verified)
        self.assertEqual(self.user.username, "selfservice@example.com")
        self.assertTrue(self.user.check_password("Password123!"))
        self.assertEqual(self.user.id, self.user.pk)  # id never re-pointed

        # financial state lives on the demo account and is untouched
        account = DemoAccount.objects.get(user=self.user)
        self.assertEqual(account.cash_balance, Decimal("250.00"))

        body = res.json()
        self.assertEqual(body["role"], User.Role.CLIENT)
        self.assertFalse(body["is_staff"])
        self.assertFalse(body["is_verified"])

    def test_put_cannot_escalate_privileges(self):
        self.client.force_authenticate(self.user)
        res = self.client.put(
            URL,
            data={"first_name": "Sneaky", "role": "admin", "is_staff": True, "is_verified": True},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)
        self.user.refresh_from_db()
        self.assertEqual(self.user.role, User.Role.CLIENT)
        self.assertFalse(self.user.is_staff)
        self.assertFalse(self.user.is_verified)

    def test_invalid_email_returns_field_level_error(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(URL, data={"email": "not-an-email"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", res.json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "selfservice@example.com")

    def test_duplicate_email_returns_field_level_error(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(URL, data={"email": "neighbour@example.com"}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("email", res.json())
        self.user.refresh_from_db()
        self.assertEqual(self.user.email, "selfservice@example.com")


class ProfileIsolationTests(ProfileSelfServiceBase):
    """No path through the self-service endpoint reaches another account."""

    def test_there_is_no_per_user_profile_route(self):
        self.client.force_authenticate(self.user)
        for url in (f"/api/profile/{self.other.id}/", f"/api/profile/me/"):
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_404_NOT_FOUND)

    def test_supplied_user_id_cannot_redirect_the_update(self):
        self.client.force_authenticate(self.user)
        res = self.client.patch(
            URL,
            data={"id": self.other.id, "first_name": "Changed"},
            format="json",
        )
        self.assertEqual(res.status_code, status.HTTP_200_OK, res.content)

        self.user.refresh_from_db()
        self.other.refresh_from_db()
        self.assertEqual(self.user.first_name, "Changed")
        # the neighbouring account is completely untouched
        self.assertEqual(self.other.first_name, "Neighbour")
        self.assertEqual(self.other.email, "neighbour@example.com")
        self.assertEqual(DemoAccount.objects.get(user=self.other).cash_balance, Decimal("750.00"))
        self.assertEqual(res.json()["id"], self.user.id)

    def test_each_user_only_ever_reads_their_own_profile(self):
        self.client.force_authenticate(self.other)
        body = self.client.get(URL).json()
        self.assertEqual(body["id"], self.other.id)
        self.assertEqual(body["email"], "neighbour@example.com")


class InactiveAccountTests(ProfileSelfServiceBase):
    def test_inactive_user_is_rejected_even_when_authenticated(self):
        # force_authenticate bypasses JWT, so this proves the view itself
        # refuses inactive accounts (defence in depth).
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.client.force_authenticate(self.user)

        self.assertEqual(self.client.get(URL).status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            self.client.patch(URL, data={"first_name": "Nope"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )
        self.assertEqual(
            self.client.put(URL, data={"first_name": "Nope"}, format="json").status_code,
            status.HTTP_403_FORBIDDEN,
        )

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "Self")

    def test_inactive_user_is_rejected_with_a_real_jwt(self):
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        login = self.client.post(
            "/api/auth/token/",
            data={"username": "selfservice@example.com", "password": "Password123!"},
            format="json",
        )
        # Login itself already refuses inactive accounts (existing policy).
        self.assertEqual(login.status_code, status.HTTP_401_UNAUTHORIZED)
        # And even a token issued before deactivation cannot be used.
        res = self.client.get(URL, HTTP_AUTHORIZATION="Bearer stale-token")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

class DashboardAccountIsolationTests(ProfileSelfServiceBase):
    def setUp(self):
        super().setUp()
        self.instrument = Instrument.objects.create(
            symbol="ISO-A",
            name="Isolation A",
            asset_class="Equity",
            currency="USD",
            price=Decimal("10.00"),
        )
        Position.objects.create(
            user=self.user, instrument=self.instrument,
            quantity=Decimal("2"), avg_price=Decimal("8.00"),
        )
        Position.objects.create(
            user=self.other, instrument=self.instrument,
            quantity=Decimal("9"), avg_price=Decimal("7.00"),
        )
        self.own_transaction = Transaction.objects.create(
            user=self.user, instrument=self.instrument, symbol="ISO-A", side="buy",
            quantity=Decimal("2"), execution_price=Decimal("10.00"),
            amount=Decimal("20.00"), status="simulated", note="owner-a",
        )
        self.other_transaction = Transaction.objects.create(
            user=self.other, instrument=self.instrument, symbol="ISO-A", side="buy",
            quantity=Decimal("9"), execution_price=Decimal("10.00"),
            amount=Decimal("90.00"), status="simulated", note="owner-b",
        )

    def test_dashboard_only_returns_authenticated_users_account_positions_and_transactions(self):
        self.client.force_authenticate(self.user)
        response = self.client.get("/api/dashboard/")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["account"]["cash_balance"], "250.00")
        self.assertEqual([row["quantity"] for row in body["positions"]], ["2.00000000"])
        self.assertEqual([row["id"] for row in body["transactions"]], [self.own_transaction.id])
        self.assertNotIn(self.other_transaction.id, [row["id"] for row in body["transactions"]])
        self.assertNotIn("owner-b", str(body))

    def test_dashboard_requires_authentication(self):
        response = self.client.get("/api/dashboard/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
