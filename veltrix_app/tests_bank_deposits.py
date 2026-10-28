from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from veltrix_app.models import (
    Asset,
    BankDeposit,
    ChainTransfer,
    Deposit,
    DepositProvider,
    Network,
    ReconciliationDiscrepancy,
    ReconciliationRun,
)
from veltrix_app.services import ledger
from veltrix_app.services.deposits import reverse_deposit


SETTINGS = dict(
    FUNDING_DEPOSITS_ENABLED=True,
    FUNDING_REQUIRE_KYC=False,
    BANK_TRANSFERS_ENABLED=True,
    BANK_TRANSFER_CURRENCY="USD",
    CRYPTO_DEPOSIT_MODE="nowpayments",
)


@override_settings(**SETTINGS)
class BankDepositTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="bank-user", email="bank@example.com", password="secret"
        )
        self.admin = get_user_model().objects.create_user(
            username="bank-admin", email="bankadmin@example.com",
            password="secret", role="admin",
        )
        self.asset = Asset.objects.create(
            code="USD", display_name="US Dollar", decimals=2, is_active=True
        )
        self.provider = DepositProvider.objects.create(
            code="bank_manual", display_name="Bank transfer", rail=DepositProvider.Rail.BANK,
            is_active=True, is_test=False,
        )
        self.provider.supported_assets.add(self.asset)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)

    def create_bank(self, amount="10.00", key="bank-intent-1"):
        response = self.client.post(
            "/api/deposits/bank/", {"amount": amount}, format="json",
            HTTP_IDEMPOTENCY_KEY=key,
        )
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def test_creation_returns_no_invented_account_details(self):
        data = self.create_bank()
        self.assertEqual(data["rail"], "bank")
        self.assertFalse(data["instructions"]["account_details_provided"])
        self.assertNotIn("account_number", data["instructions"])
        self.assertIn("support", data["instructions"]["message"].lower())
        self.assertEqual(data["status"], Deposit.Status.PENDING)
        self.assertEqual(BankDeposit.objects.count(), 1)

    def test_admin_approval_records_verified_amount_and_credits_once(self):
        data = self.create_bank()
        payload = {
            "amount": "9.50",
            "currency": "USD",
            "reference": data["reference"],
            "verification_source": "Bank statement 2026-10-25 row 12",
            "reason": "Verified independently against the statement",
        }
        response = self.admin_client.post(
            f"/api/admin/bank-deposits/{data['id']}/approve/", payload, format="json"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_credited"])
        deposit = Deposit.objects.get(pk=data["id"])
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("9.50"))
        self.assertEqual(deposit.received_amount, Decimal("9.50"))
        bank = deposit.bank
        self.assertEqual(bank.verified_amount, Decimal("9.50"))
        self.assertEqual(bank.approved_by, self.admin)
        self.assertEqual(bank.verification_source, payload["verification_source"])
        # Retrying the approval must not double-credit.
        again = self.admin_client.post(
            f"/api/admin/bank-deposits/{data['id']}/approve/", payload, format="json"
        )
        self.assertEqual(again.status_code, 200)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("9.50"))

    def test_global_funding_switch_blocks_new_bank_credits(self):
        data = self.create_bank()
        payload = {
            "amount": "10.00", "currency": "USD", "reference": data["reference"],
            "verification_source": "Statement", "reason": "Verified",
        }
        with override_settings(FUNDING_DEPOSITS_ENABLED=False):
            response = self.admin_client.post(
                f"/api/admin/bank-deposits/{data['id']}/approve/", payload, format="json"
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))
        self.assertFalse(Deposit.objects.get(pk=data["id"]).is_credited)

    def test_bank_reversal_marks_bank_deposit_reversed(self):
        data = self.create_bank()
        payload = {
            "amount": "10.00", "currency": "USD", "reference": data["reference"],
            "verification_source": "Statement", "reason": "Verified",
        }
        response = self.admin_client.post(
            f"/api/admin/bank-deposits/{data['id']}/approve/", payload, format="json"
        )
        self.assertEqual(response.status_code, 200)
        deposit = Deposit.objects.get(pk=data["id"])
        reverse_deposit(deposit, reason="Bank reversed after settlement", actor=self.admin)
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Deposit.Status.REVERSED)
        self.assertIsNotNone(deposit.bank.reversed_at)

    def test_approval_requires_exact_reference_currency_and_evidence(self):
        data = self.create_bank()
        base = {
            "amount": "10.00", "currency": "USD",
            "reference": data["reference"], "verification_source": "Statement",
            "reason": "Checked",
        }
        for change in (
            {"reference": "VELT-WRONG"},
            {"currency": "EUR"},
            {"verification_source": ""},
        ):
            with self.subTest(change=change):
                response = self.admin_client.post(
                    f"/api/admin/bank-deposits/{data['id']}/approve/",
                    {**base, **change}, format="json",
                )
                self.assertEqual(response.status_code, 400)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))

    def test_rejection_is_audited_and_cannot_credit(self):
        data = self.create_bank()
        response = self.admin_client.post(
            f"/api/admin/bank-deposits/{data['id']}/reject/",
            {"reason": "No matching credit on the statement"}, format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        deposit = Deposit.objects.get(pk=data["id"])
        self.assertEqual(deposit.status, Deposit.Status.REJECTED)
        self.assertEqual(deposit.bank.rejected_by, self.admin)
        self.assertFalse(deposit.is_credited)

    def test_staff_administrator_without_admin_role_can_approve(self):
        data = self.create_bank()
        staff = get_user_model().objects.create_user(
            username="bank-staff", email="bankstaff@example.com",
            password="secret", is_staff=True,
        )
        staff_client = APIClient()
        staff_client.force_authenticate(staff)
        response = staff_client.post(
            f"/api/admin/bank-deposits/{data['id']}/approve/",
            {
                "amount": "10.00", "currency": "USD", "reference": data["reference"],
                "verification_source": "Statement reviewed by staff",
                "reason": "Independent staff review",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["is_credited"])

    def test_verified_amount_with_excess_currency_precision_is_rejected(self):
        data = self.create_bank()
        response = self.admin_client.post(
            f"/api/admin/bank-deposits/{data['id']}/approve/",
            {
                "amount": "9.505", "currency": "USD", "reference": data["reference"],
                "verification_source": "Statement", "reason": "Precision test",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))

    def test_oversized_verified_amount_is_rejected_before_storage(self):
        data = self.create_bank()
        response = self.admin_client.post(
            f"/api/admin/bank-deposits/{data['id']}/approve/",
            {
                "amount": "100000000000000000000", "currency": "USD",
                "reference": data["reference"], "verification_source": "Statement",
                "reason": "Invalid oversized amount",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))

    def test_client_cannot_use_admin_bank_endpoints(self):
        data = self.create_bank()
        response = self.client.post(
            f"/api/admin/bank-deposits/{data['id']}/approve/", {}, format="json"
        )
        self.assertIn(response.status_code, (401, 403))

    def test_admin_deposit_list_filters_bank_review(self):
        data = self.create_bank()
        response = self.admin_client.get("/api/admin/deposits/?rail=bank")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["count"], 1)
        detail = self.admin_client.get(f"/api/admin/deposits/{data['id']}/")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.data["bank"]["provider_reference"], data["reference"])


@override_settings(**SETTINGS)
class ReconciliationTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_user(
            username="recon-admin", email="recon@example.com",
            password="secret", role="admin",
        )
        self.asset = Asset.objects.create(
            code="USDT", display_name="Tether", decimals=6,
            asset_class=Asset.AssetClass.CRYPTO,
        )
        self.network = Network.objects.create(
            asset=self.asset, chain="TRON", token_standard="TRC20",
            contract_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            decimals=6, is_testnet=False, is_active=True,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_unallocated_transfer_discrepancy_is_idempotent(self):
        ChainTransfer.objects.create(
            network=self.network, txid="a" * 64, event_index=0, block_number=42,
            contract_address=self.network.contract_address,
            sender_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
            recipient_address="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
            amount_base_units=Decimal("1000000"), amount=Decimal("1.000000"),
            status=ChainTransfer.Status.MANUAL_REVIEW,
        )
        first = self.client.post("/api/admin/reconciliation/", {}, format="json")
        second = self.client.post("/api/admin/reconciliation/", {}, format="json")
        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(ReconciliationRun.objects.count(), 4)
        self.assertEqual(ReconciliationDiscrepancy.objects.count(), 1)
        discrepancy = ReconciliationDiscrepancy.objects.get()
        response = self.client.post(
            f"/api/admin/reconciliation/discrepancies/{discrepancy.pk}/resolve/",
            {"resolution": "Called the sender and verified the transfer."}, format="json",
        )
        self.assertEqual(response.status_code, 200)
        discrepancy.refresh_from_db()
        self.assertEqual(discrepancy.status, ReconciliationDiscrepancy.Status.RESOLVED)
        self.assertEqual(discrepancy.resolved_by, self.admin)
        original_resolution = discrepancy.resolution
        overwrite = self.client.post(
            f"/api/admin/reconciliation/discrepancies/{discrepancy.pk}/resolve/",
            {"resolution": "A different later note.", "ignore": True}, format="json",
        )
        self.assertEqual(overwrite.status_code, 400)
        discrepancy.refresh_from_db()
        self.assertEqual(discrepancy.status, ReconciliationDiscrepancy.Status.RESOLVED)
        self.assertEqual(discrepancy.resolution, original_resolution)

    def test_non_admin_cannot_run_or_read_reconciliation(self):
        user = get_user_model().objects.create_user(username="plain", password="secret")
        client = APIClient()
        client.force_authenticate(user)
        self.assertEqual(client.get("/api/admin/reconciliation/").status_code, 403)
        self.assertEqual(client.post("/api/admin/reconciliation/", {}, format="json").status_code, 403)
