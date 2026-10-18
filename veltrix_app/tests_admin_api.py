from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APIClient

from veltrix_app.models import AuditLog, Conversation, DemoAccount, Instrument, Message, Transaction

User = get_user_model()


class AdminAPIBase(TestCase):
    """Shared setup: one admin, one client account, an instrument and a trade."""

    def setUp(self):
        self.client = APIClient()
        self.admin = User.objects.create_user(
            username="admin@example.com",
            email="admin@example.com",
            password="Password123!",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
            is_verified=True,
        )
        self.client_user = User.objects.create_user(
            username="user@example.com",
            email="user@example.com",
            password="Password123!",
            role=User.Role.CLIENT,
        )
        DemoAccount.objects.create(user=self.client_user, currency="USD", cash_balance="5000.00")
        self.instrument = Instrument.objects.create(
            symbol="AAPL",
            name="Apple Inc.",
            asset_class="Equity",
            currency="USD",
            price=Decimal("210.00"),
            change_percent=Decimal("0.5"),
            is_active=True,
        )
        self.inactive_instrument = Instrument.objects.create(
            symbol="OLDX",
            name="Old Symbol",
            asset_class="Equity",
            currency="USD",
            price=Decimal("1.00"),
            is_active=False,
        )
        Transaction.objects.create(
            user=self.client_user,
            instrument=self.instrument,
            symbol="AAPL",
            side="buy",
            quantity=Decimal("2"),
            execution_price=Decimal("210.00"),
            amount=Decimal("420.00"),
            status="simulated",
        )

    def as_admin(self):
        self.client.force_authenticate(user=self.admin)

    def as_client(self):
        self.client.force_authenticate(user=self.client_user)


class AdminPermissionTests(AdminAPIBase):
    """Every /api/admin/ endpoint must deny ordinary and anonymous users."""

    ENDPOINTS = (
        "/api/admin/stats/",
        "/api/admin/users/",
        "/api/admin/transactions/",
        "/api/admin/instruments/",
        "/api/admin/audit/",
    )

    def test_anonymous_gets_401_on_all_endpoints(self):
        for url in self.ENDPOINTS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_401_UNAUTHORIZED)

    def test_client_gets_403_on_all_endpoints(self):
        self.as_client()
        for url in self.ENDPOINTS:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, status.HTTP_403_FORBIDDEN)

    def test_client_gets_403_on_detail_and_mutating_endpoints(self):
        self.as_client()
        cases = (
            (self.client.get, f"/api/admin/users/{self.client_user.id}/"),
            (self.client.patch, f"/api/admin/users/{self.client_user.id}/"),
            (self.client.get, f"/api/admin/instruments/{self.instrument.id}/"),
            (self.client.patch, f"/api/admin/instruments/{self.instrument.id}/"),
        )
        for call, url in cases:
            with self.subTest(url=url, method=call.__name__):
                self.assertEqual(call(url, {}, format="json").status_code, status.HTTP_403_FORBIDDEN)

    def test_admin_role_without_staff_flag_is_allowed(self):
        role_only = User.objects.create_user(
            username="roleadmin@example.com",
            email="roleadmin@example.com",
            password="Password123!",
            role=User.Role.ADMIN,
        )
        self.client.force_authenticate(user=role_only)
        self.assertEqual(self.client.get("/api/admin/stats/").status_code, status.HTTP_200_OK)

    def test_deactivated_admin_is_denied(self):
        self.admin.is_active = False
        self.admin.save(update_fields=["is_active"])
        self.as_admin()
        self.assertEqual(self.client.get("/api/admin/stats/").status_code, status.HTTP_403_FORBIDDEN)


class AdminStatsTests(AdminAPIBase):
    def test_stats_contain_expected_aggregates(self):
        self.as_admin()
        response = self.client.get("/api/admin/stats/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["users"]["total"], 2)
        self.assertEqual(data["users"]["active"], 2)
        self.assertEqual(data["users"]["verified"], 1)
        self.assertEqual(data["users"]["admins"], 1)
        self.assertEqual(data["transactions"]["total"], 1)
        self.assertEqual(data["transactions"]["total_volume"], "420.00")
        self.assertEqual(data["transactions"]["by_side"], {"buy": 1})
        self.assertEqual(data["accounts"]["total_cash_balance"], "5000.00")
        self.assertIn("open_conversations", data["support"])


class AdminUserListTests(AdminAPIBase):
    def test_list_returns_users_without_sensitive_fields(self):
        self.as_admin()
        response = self.client.get("/api/admin/users/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["count"], 2)
        row = next(r for r in body["results"] if r["id"] == self.client_user.id)
        self.assertNotIn("password", row)
        self.assertEqual(row["cash_balance"], "5000.00")
        self.assertEqual(row["transaction_count"], 1)

    def test_search_matches_email_and_name(self):
        self.as_admin()
        response = self.client.get("/api/admin/users/", {"search": "user@example"})
        self.assertEqual(response.json()["count"], 1)
        response = self.client.get("/api/admin/users/", {"search": "no-such-user"})
        self.assertEqual(response.json()["count"], 0)

    def test_filters(self):
        self.as_admin()
        self.assertEqual(self.client.get("/api/admin/users/", {"role": "client"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/users/", {"is_verified": "true"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/users/", {"is_active": "false"}).json()["count"], 0)
        self.assertEqual(self.client.get("/api/admin/users/", {"is_staff": "true"}).json()["count"], 1)

    def test_pagination(self):
        for i in range(5):
            User.objects.create_user(
                username=f"bulk{i}@example.com", email=f"bulk{i}@example.com", password="Password123!"
            )
        self.as_admin()
        page1 = self.client.get("/api/admin/users/", {"page": 1, "page_size": 3}).json()
        page2 = self.client.get("/api/admin/users/", {"page": 2, "page_size": 3}).json()
        self.assertEqual(page1["count"], 7)
        self.assertEqual(len(page1["results"]), 3)
        self.assertEqual(len(page2["results"]), 3)


class AdminUserDetailTests(AdminAPIBase):
    def test_detail_includes_positions_and_transactions(self):
        self.as_admin()
        response = self.client.get(f"/api/admin/users/{self.client_user.id}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.json()
        self.assertEqual(data["email"], "user@example.com")
        self.assertEqual(data["positions"], [])
        self.assertEqual(len(data["recent_transactions"]), 1)
        self.assertNotIn("password", data)

    def test_detail_404_for_missing_user(self):
        self.as_admin()
        self.assertEqual(self.client.get("/api/admin/users/99999/").status_code, status.HTTP_404_NOT_FOUND)


class AdminUserPatchTests(AdminAPIBase):
    def test_admin_can_deactivate_and_reactivate_user(self):
        self.as_admin()
        response = self.client.patch(
            f"/api/admin/users/{self.client_user.id}/", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client_user.refresh_from_db()
        self.assertFalse(self.client_user.is_active)

        response = self.client.patch(
            f"/api/admin/users/{self.client_user.id}/", {"is_active": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.is_active)

    def test_admin_can_toggle_verification(self):
        self.as_admin()
        response = self.client.patch(
            f"/api/admin/users/{self.client_user.id}/", {"is_verified": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.is_verified)

    def test_privileged_fields_are_ignored_not_applied(self):
        self.as_admin()
        response = self.client.patch(
            f"/api/admin/users/{self.client_user.id}/",
            {"is_verified": True, "is_superuser": True, "role": "admin", "is_staff": True, "email": "hijack@example.com"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.client_user.refresh_from_db()
        self.assertTrue(self.client_user.is_verified)  # the one valid field applied
        self.assertFalse(self.client_user.is_superuser)
        self.assertFalse(self.client_user.is_staff)
        self.assertEqual(self.client_user.role, User.Role.CLIENT)
        self.assertEqual(self.client_user.email, "user@example.com")

    def test_cannot_deactivate_self(self):
        self.as_admin()
        response = self.client.patch(f"/api/admin/users/{self.admin.id}/", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_cannot_deactivate_last_superuser(self):
        other_admin = User.objects.create_user(
            username="other@example.com",
            email="other@example.com",
            password="Password123!",
            role=User.Role.ADMIN,
            is_staff=True,
        )
        self.as_admin()
        response = self.client.patch(f"/api/admin/users/{self.admin.id}/", {"is_active": False}, format="json")
        # self-deactivation is rejected before the last-superuser check
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # a staff admin cannot remove the only superuser either
        self.client.force_authenticate(user=other_admin)
        response = self.client.patch(f"/api/admin/users/{self.admin.id}/", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_superuser_deactivation_allowed_when_another_superuser_exists(self):
        second = User.objects.create_user(
            username="second@example.com",
            email="second@example.com",
            password="Password123!",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
        )
        self.as_admin()
        response = self.client.patch(f"/api/admin/users/{second.id}/", {"is_active": False}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        second.refresh_from_db()
        self.assertFalse(second.is_active)

    def test_patch_writes_audit_log(self):
        self.as_admin()
        self.client.patch(f"/api/admin/users/{self.client_user.id}/", {"is_active": False}, format="json")
        entry = AuditLog.objects.filter(action="admin.user.deactivate").first()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.actor_id, self.admin.id)
        self.assertEqual(entry.user_id, self.client_user.id)

    def test_empty_patch_is_400(self):
        self.as_admin()
        response = self.client.patch(f"/api/admin/users/{self.client_user.id}/", {}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class AdminTransactionListTests(AdminAPIBase):
    def test_list_is_read_only_and_scoped_to_platform(self):
        self.as_admin()
        response = self.client.get("/api/admin/transactions/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertEqual(body["count"], 1)
        row = body["results"][0]
        self.assertEqual(row["user_email"], "user@example.com")
        self.assertEqual(row["symbol"], "AAPL")
        self.assertNotIn("password", row)

    def test_transactions_have_no_detail_or_write_routes(self):
        self.as_admin()
        tx = Transaction.objects.first()
        self.assertEqual(self.client.get(f"/api/admin/transactions/{tx.id}/").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.patch(f"/api/admin/transactions/{tx.id}/", {}, format="json").status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.delete(f"/api/admin/transactions/{tx.id}/").status_code, status.HTTP_404_NOT_FOUND)

    def test_filters(self):
        Transaction.objects.create(
            user=self.admin, symbol="USD", side="topup", quantity=Decimal("0"),
            execution_price=Decimal("1"), amount=Decimal("100.00"), status="simulated",
        )
        self.as_admin()
        self.assertEqual(self.client.get("/api/admin/transactions/", {"side": "topup"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"side": "buy"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"status": "simulated"}).json()["count"], 2)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"user_email": "USER@example.com"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"search": "AAPL"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"search": "nope"}).json()["count"], 0)

    def test_date_range_filters(self):
        self.as_admin()
        future = "2999-01-01"
        past = "2000-01-01"
        self.assertEqual(self.client.get("/api/admin/transactions/", {"date_from": future}).json()["count"], 0)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"date_from": past}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"date_to": past}).json()["count"], 0)
        self.assertEqual(self.client.get("/api/admin/transactions/", {"date_from": past, "date_to": future}).json()["count"], 1)

    def test_invalid_date_is_ignored_not_500(self):
        self.as_admin()
        response = self.client.get("/api/admin/transactions/", {"date_from": "not-a-date"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.json()["count"], 1)


class AdminInstrumentTests(AdminAPIBase):
    def test_admin_sees_inactive_instruments(self):
        self.as_admin()
        body = self.client.get("/api/admin/instruments/").json()
        symbols = {r["symbol"] for r in body["results"]}
        self.assertIn("AAPL", symbols)
        self.assertIn("OLDX", symbols)

    def test_customer_public_endpoint_still_hides_inactive(self):
        self.as_client()
        response = self.client.get("/api/instruments/")
        symbols = {r["symbol"] for r in response.json()}
        self.assertNotIn("OLDX", symbols)

    def test_filters_and_search(self):
        self.as_admin()
        self.assertEqual(self.client.get("/api/admin/instruments/", {"search": "old"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/instruments/", {"is_active": "false"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/instruments/", {"asset_class": "equity"}).json()["count"], 2)

    def test_deactivate_and_reactivate_instrument_with_audit(self):
        self.as_admin()
        response = self.client.patch(
            f"/api/admin/instruments/{self.instrument.id}/", {"is_active": False}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.instrument.refresh_from_db()
        self.assertFalse(self.instrument.is_active)
        entry = AuditLog.objects.filter(action="admin.instrument.deactivate").first()
        self.assertIsNotNone(entry)
        self.assertIn("AAPL", entry.details)

        response = self.client.patch(
            f"/api/admin/instruments/{self.instrument.id}/", {"is_active": True}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.instrument.refresh_from_db()
        self.assertTrue(self.instrument.is_active)

    def test_price_and_symbol_are_not_writable(self):
        self.as_admin()
        response = self.client.patch(
            f"/api/admin/instruments/{self.instrument.id}/",
            {"price": "1.00", "symbol": "HACK", "name": "Hacked"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)  # no writable field present


class AdminAuditLogTests(AdminAPIBase):
    def test_shows_admin_actions_hides_request_noise(self):
        AuditLog.objects.create(actor=self.admin, user=self.admin, action="admin.user.update", details="test")
        AuditLog.objects.create(actor=self.client_user, user=self.client_user, action="request:POST", details="/api/orders/")
        self.as_admin()
        body = self.client.get("/api/admin/audit/").json()
        actions = [r["action"] for r in body["results"]]
        self.assertIn("admin.user.update", actions)
        self.assertNotIn("request:POST", actions)

        body = self.client.get("/api/admin/audit/", {"include_requests": "1"}).json()
        actions = [r["action"] for r in body["results"]]
        self.assertIn("request:POST", actions)

    def test_search_and_actor_filter(self):
        AuditLog.objects.create(actor=self.admin, user=self.admin, action="admin.instrument.deactivate", details="instrument=AAPL")
        AuditLog.objects.create(actor=self.client_user, user=self.client_user, action="admin.user.update", details="other")
        self.as_admin()
        self.assertEqual(self.client.get("/api/admin/audit/", {"search": "instrument"}).json()["count"], 1)
        self.assertEqual(self.client.get("/api/admin/audit/", {"actor": str(self.admin.id)}).json()["count"], 1)

    def test_serialized_entry_never_includes_password_data(self):
        AuditLog.objects.create(actor=self.admin, user=self.admin, action="admin.user.update", details="x")
        self.as_admin()
        row = self.client.get("/api/admin/audit/").json()["results"][0]
        self.assertNotIn("password", row)
        self.assertEqual(row["actor_email"], "admin@example.com")


class ExistingSupportEndpointsStillWorkForAdmin(AdminAPIBase):
    """Regression: the support conversation endpoints power the admin support UI."""

    def test_admin_sees_all_conversations_and_can_resolve(self):
        conv = Conversation.objects.create(user=self.client_user, subject="Help")
        Message.objects.create(conversation=conv, sender=self.client_user, content="Hello")
        self.as_admin()
        body = self.client.get("/api/conversations/").json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["user"]["email"], "user@example.com")
        response = self.client.post(f"/api/conversations/{conv.id}/resolve/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        conv.refresh_from_db()
        self.assertFalse(conv.is_active)

    def test_customer_conversation_list_still_works_with_user_field(self):
        conv = Conversation.objects.create(user=self.client_user, subject="Mine")
        Message.objects.create(conversation=conv, sender=self.client_user, content="Hi")
        self.as_client()
        body = self.client.get("/api/conversations/").json()
        self.assertEqual(len(body), 1)
        self.assertEqual(body[0]["user"]["email"], "user@example.com")

    def test_customer_admin_endpoints_remain_unaffected(self):
        # Existing customer flows still behave for a plain client.
        self.as_client()
        self.assertEqual(self.client.get("/api/dashboard/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.get("/api/watchlist/").status_code, status.HTTP_200_OK)
        self.assertEqual(self.client.post("/api/orders/", {}, format="json").status_code, status.HTTP_400_BAD_REQUEST)
