"""JWT login tests for the client-frontend authentication flow.

Covers the documented root cause of the admin login failure: the client
frontend always posts an *email* to ``/api/auth/token/``, but the production
administrator account is provisioned with a username that is NOT an email
(``username='veltrixadmin'``, ``email='veltrix@gmail.com'`` via the
ensure_superuser workflow). Django Admin authenticates by username, so it
works; the API lookup was username-only, so the same valid credentials failed
with the generic SimpleJWT error that the frontend renders as "no account".

These tests also pin the existing security behaviour: generic 401s (no account
enumeration), inactive accounts rejected, unverified accounts still able to
sign in (existing policy), and token-authenticated admin endpoint access.
"""

from django.contrib.auth import authenticate, get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from veltrix_app.models import AuditLog, DemoAccount

User = get_user_model()

ADMIN_USERNAME = "veltrixadmin"
ADMIN_EMAIL = "veltrix@gmail.com"
ADMIN_PASSWORD = "VeltrixAdmin123!"

CUSTOMER_EMAIL = "trader@example.com"
CUSTOMER_PASSWORD = "CustomerPass123!"


def login(client, username, password):
    return client.post(
        "/api/auth/token/",
        data={"username": username, "password": password},
        format="json",
    )


class AdminLoginTests(TestCase):
    """Valid admin credentials must authenticate through the client API."""

    def setUp(self):
        self.client = APIClient()
        # Same shape as the production account: username is NOT the email.
        self.admin = User.objects.create_user(
            username=ADMIN_USERNAME,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
            is_verified=True,
        )

    def test_django_style_username_authentication_still_works(self):
        # The credentials are valid: the same backend that powers Django
        # Admin's login form must resolve this account by username.
        user = authenticate(username=ADMIN_USERNAME, password=ADMIN_PASSWORD)
        self.assertIsNotNone(user)
        self.assertEqual(user.pk, self.admin.pk)

    def test_admin_can_login_with_the_email_the_frontend_sends(self):
        response = login(self.client, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertIn("access", body)
        self.assertIn("refresh", body)

    def test_admin_can_login_with_username(self):
        response = login(self.client, ADMIN_USERNAME, ADMIN_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_admin_login_is_case_and_whitespace_insensitive(self):
        for identifier in (
            ADMIN_EMAIL.upper(),
            "  Veltrix@Gmail.com ",
            ADMIN_USERNAME.upper(),
        ):
            with self.subTest(identifier=identifier):
                response = login(self.client, identifier, ADMIN_PASSWORD)
                self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_login_returns_identity_and_role(self):
        response = login(self.client, ADMIN_EMAIL, ADMIN_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        user_payload = response.json().get("user")
        self.assertIsNotNone(user_payload, "login must return the authenticated identity")
        self.assertEqual(user_payload["id"], self.admin.id)
        self.assertEqual(user_payload["email"], ADMIN_EMAIL)
        self.assertEqual(user_payload["role"], User.Role.ADMIN)
        self.assertTrue(user_payload["is_staff"])
        self.assertTrue(user_payload["is_verified"])

    def test_login_response_never_contains_password_material(self):
        response = login(self.client, ADMIN_EMAIL, ADMIN_PASSWORD)
        body = response.json()
        self.assertNotIn("password", body)
        self.assertNotIn(str(self.admin.password), str(body))


class CustomerLoginTests(TestCase):
    """Existing customer login behaviour must not regress."""

    def setUp(self):
        self.client = APIClient()
        self.customer = User.objects.create_user(
            username=CUSTOMER_EMAIL,
            email=CUSTOMER_EMAIL,
            password=CUSTOMER_PASSWORD,
            role=User.Role.CLIENT,
        )

    def test_customer_login_with_email(self):
        response = login(self.client, CUSTOMER_EMAIL, CUSTOMER_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        body = response.json()
        self.assertIn("access", body)
        self.assertEqual(body["user"]["role"], User.Role.CLIENT)
        self.assertFalse(body["user"]["is_staff"])

    def test_customer_login_case_insensitive(self):
        response = login(self.client, "TRADER@EXAMPLE.COM", CUSTOMER_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)

    def test_unverified_customer_can_still_login(self):
        # Existing policy: is_verified gates KYC display, not authentication.
        self.assertFalse(self.customer.is_verified)
        response = login(self.client, CUSTOMER_EMAIL, CUSTOMER_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.assertFalse(response.json()["user"]["is_verified"])

    def test_inactive_account_cannot_login(self):
        self.customer.is_active = False
        self.customer.save(update_fields=["is_active"])
        response = login(self.client, CUSTOMER_EMAIL, CUSTOMER_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_admin_cannot_login(self):
        admin = User.objects.create_user(
            username="inactive-admin",
            email="inactive-admin@example.com",
            password=ADMIN_PASSWORD,
            role=User.Role.ADMIN,
            is_staff=True,
            is_verified=True,
            is_active=False,
        )
        response = login(self.client, admin.email, ADMIN_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class InvalidCredentialTests(TestCase):
    """Failures must stay generic: no account enumeration, no password leaks."""

    def setUp(self):
        self.client = APIClient()
        self.customer = User.objects.create_user(
            username=CUSTOMER_EMAIL,
            email=CUSTOMER_EMAIL,
            password=CUSTOMER_PASSWORD,
        )
        User.objects.create_user(
            username=ADMIN_USERNAME,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            role=User.Role.ADMIN,
            is_staff=True,
            is_verified=True,
        )

    def test_wrong_password_is_rejected(self):
        for identifier in (CUSTOMER_EMAIL, ADMIN_EMAIL, ADMIN_USERNAME):
            with self.subTest(identifier=identifier):
                response = login(self.client, identifier, "WrongPassword999!")
                self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unknown_identifier_is_rejected(self):
        response = login(self.client, "nobody@example.com", CUSTOMER_PASSWORD)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_unknown_identifier_and_wrong_password_share_the_same_detail(self):
        unknown = login(self.client, "nobody@example.com", CUSTOMER_PASSWORD)
        wrong_pw = login(self.client, CUSTOMER_EMAIL, "WrongPassword999!")
        self.assertEqual(unknown.json()["detail"], wrong_pw.json()["detail"])

    def test_error_message_does_not_claim_the_account_is_missing(self):
        response = login(self.client, "nobody@example.com", CUSTOMER_PASSWORD)
        detail = str(response.json().get("detail", "")).lower()
        for phrase in ("no account", "does not exist", "not found", "no user"):
            self.assertNotIn(phrase, detail)

    def test_missing_fields_are_a_validation_error(self):
        response = self.client.post("/api/auth/token/", data={}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AdminEndpointTokenAuthTests(TestCase):
    """Real JWT tokens (not force_authenticate) against /api/admin/."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username=ADMIN_USERNAME,
            email=ADMIN_EMAIL,
            password=ADMIN_PASSWORD,
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
            is_verified=True,
        )
        self.customer = User.objects.create_user(
            username=CUSTOMER_EMAIL,
            email=CUSTOMER_EMAIL,
            password=CUSTOMER_PASSWORD,
            role=User.Role.CLIENT,
        )
        DemoAccount.objects.create(user=self.customer, currency="USD", cash_balance="100.00")

    def _token_for(self, identifier, password):
        response = login(self.client, identifier, password)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        return response.json()["access"]

    def test_admin_login_grants_admin_api_access(self):
        token = self._token_for(ADMIN_EMAIL, ADMIN_PASSWORD)
        for url in ("/api/admin/stats/", "/api/admin/users/", "/api/admin/transactions/", "/api/admin/audit/"):
            with self.subTest(url=url):
                response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {token}")
                self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_customer_token_is_denied_admin_api(self):
        token = self._token_for(CUSTOMER_EMAIL, CUSTOMER_PASSWORD)
        for url in ("/api/admin/stats/", "/api/admin/users/", "/api/admin/transactions/", "/api/admin/audit/"):
            with self.subTest(url=url):
                response = self.client.get(url, HTTP_AUTHORIZATION=f"Bearer {token}")
                self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_missing_token_is_unauthorized(self):
        self.assertEqual(self.client.get("/api/admin/stats/").status_code, status.HTTP_401_UNAUTHORIZED)

    def test_garbage_token_is_unauthorized(self):
        response = self.client.get("/api/admin/stats/", HTTP_AUTHORIZATION="Bearer not-a-real-token")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_expired_style_invalid_token_is_unauthorized(self):
        response = self.client.get("/api/admin/stats/", HTTP_AUTHORIZATION="Bearer ")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_admin_can_toggle_customer_state_with_audit_log(self):
        token = self._token_for(ADMIN_EMAIL, ADMIN_PASSWORD)
        response = self.client.patch(
            f"/api/admin/users/{self.customer.id}/",
            {"is_verified": True},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.content)
        self.customer.refresh_from_db()
        self.assertTrue(self.customer.is_verified)
        self.assertTrue(
            AuditLog.objects.filter(actor_id=self.admin.id, user_id=self.customer.id).exists()
        )
        self.assertNotIn("password", response.json())
