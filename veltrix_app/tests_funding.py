from decimal import Decimal

from django.contrib.auth import get_user_model
from django.db import DatabaseError, IntegrityError, transaction
from django.test import TestCase, override_settings
from unittest.mock import patch

from veltrix_app.models import (
    Asset,
    AssetBalance,
    AuditLog,
    BankDeposit,
    CardDeposit,
    Conversation,
    CryptoDeposit,
    Deposit,
    DepositProvider,
    Job,
    KycProfile,
    LedgerEntry,
    Message,
    Network,
)
from veltrix_app.services import ledger
from veltrix_app.services.kyc import approve_profile, ensure_profile


User = get_user_model()


class FundingModelMigrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="funding-client@example.com",
            email="funding-client@example.com",
            password="Password123!",
        )
        self.asset = Asset.objects.create(code="USD", display_name="US Dollar")
        self.provider = DepositProvider.objects.create(
            code="local-bank", display_name="Local Bank", rail=DepositProvider.Rail.BANK
        )

    def make_deposit(self, idempotency_key, provider_ref=""):
        return Deposit.objects.create(
            user=self.user,
            rail=Deposit.Rail.BANK,
            asset=self.asset,
            provider=self.provider,
            expected_amount=Decimal("25.00"),
            idempotency_key=idempotency_key,
            provider_ref=provider_ref,
        )

    def test_user_freeze_and_kyc_defaults_are_migrated(self):
        self.assertFalse(self.user.funds_frozen)
        profile = ensure_profile(self.user)
        self.assertEqual(profile.status, KycProfile.Status.UNVERIFIED)
        self.assertEqual(ensure_profile(self.user).pk, profile.pk)
        approve_profile(profile)
        profile.refresh_from_db()
        self.assertTrue(profile.is_approved)

    def test_optional_provider_identifiers_allow_multiple_blank_records(self):
        first = self.make_deposit("bank-1")
        second = self.make_deposit("bank-2")
        BankDeposit.objects.create(deposit=first, provider_account="", provider_reference="")
        BankDeposit.objects.create(deposit=second, provider_account="", provider_reference="")
        CardDeposit.objects.create(deposit=first)
        CardDeposit.objects.create(deposit=second)
        network = Network.objects.create(asset=self.asset, chain="test-chain")
        CryptoDeposit.objects.create(deposit=first, network=network)
        CryptoDeposit.objects.create(deposit=second, network=network)
        Job.objects.create(kind=Job.Kind.DETECT)
        Job.objects.create(kind=Job.Kind.DETECT)
        self.assertEqual(BankDeposit.objects.count(), 2)
        self.assertEqual(CardDeposit.objects.count(), 2)
        self.assertEqual(CryptoDeposit.objects.count(), 2)
        self.assertEqual(Job.objects.count(), 2)

    def test_nonblank_deposit_provider_reference_remains_unique(self):
        self.make_deposit("bank-1", provider_ref="provider-ref-1")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.make_deposit("bank-2", provider_ref="provider-ref-1")

    @override_settings(FUNDING_DEPOSITS_ENABLED=True, FUNDING_REQUIRE_KYC=True)




    def test_ledger_entry_updates_balance_cache_and_is_idempotent(self):
        treasury = ledger.treasury_account(self.asset)
        customer = ledger.customer_account(self.asset, self.user)
        postings = [
            ledger.Posting(treasury, "D", Decimal("10.00")),
            ledger.Posting(customer, "C", Decimal("10.00"), bucket="available"),
        ]
        first = ledger.record_entry(
            "adjustment", "funding-test-1", postings, asset=self.asset
        )
        retry = ledger.record_entry(
            "adjustment", "funding-test-1", postings, asset=self.asset
        )
        self.assertEqual(first.pk, retry.pk)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("10.00"))
        self.assertEqual(ledger.get_balance(self.user, self.asset).available, Decimal("10.00"))


class LedgerSafetyRegressionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ledger-safety@example.com",
            email="ledger-safety@example.com",
            password="Password123!",
        )
        self.usd = Asset.objects.create(code="USD", display_name="US Dollar")
        self.ngn = Asset.objects.create(code="NGN", display_name="Nigerian Naira")

    def _leg(self, asset, reference, amount=Decimal("10.00")):
        treasury = ledger.treasury_account(asset)
        customer = ledger.customer_account(asset, self.user)
        return reference, [
            ledger.Posting(treasury, "D", amount),
            ledger.Posting(customer, "C", amount, bucket="available"),
        ]

    def test_reconciliation_mismatch_freezes_and_audits_account(self):
        cached = ledger.get_balance(self.user, self.usd)
        cached.available = Decimal("3.00")
        cached.save(update_fields=["available"])

        problems = ledger.reconcile_balances()

        self.user.refresh_from_db()
        self.assertTrue(self.user.funds_frozen)
        self.assertEqual(len(problems), 1)
        self.assertEqual(problems[0][0], self.user.pk)
        event = AuditLog.objects.get(action="ledger.balance_mismatch")
        self.assertIn("asset=USD", event.details)
        self.assertIn("account frozen", event.details)

    def test_reconciliation_preserves_existing_freeze(self):
        self.user.funds_frozen = True
        self.user.save(update_fields=["funds_frozen"])
        cached = ledger.get_balance(self.user, self.usd)
        cached.available = Decimal("3.00")
        cached.save(update_fields=["available"])

        ledger.reconcile_balances()

        self.user.refresh_from_db()
        self.assertTrue(self.user.funds_frozen)
        self.assertTrue(AuditLog.objects.filter(action="ledger.balance_mismatch").exists())

    def test_reconciliation_database_error_is_not_reported_as_handled(self):
        cached = ledger.get_balance(self.user, self.usd)
        cached.available = Decimal("3.00")
        cached.save(update_fields=["available"])

        with patch.object(AuditLog.objects, "create", side_effect=DatabaseError("audit write failed")):
            with self.assertRaises(DatabaseError):
                ledger.reconcile_balances()

        self.user.refresh_from_db()
        self.assertFalse(self.user.funds_frozen)

    def _credited_deposit(self, key="deposit-safe"):
        provider = DepositProvider.objects.create(
            code=f"provider-{key}", display_name="Local test provider", rail=DepositProvider.Rail.BANK
        )
        deposit = Deposit.objects.create(
            user=self.user,
            rail=Deposit.Rail.BANK,
            asset=self.usd,
            provider=provider,
            expected_amount=Decimal("10.00"),
            received_amount=Decimal("10.00"),
            idempotency_key=key,
            status=Deposit.Status.VERIFIED,
        )
        return credit_deposit(deposit)





    def test_multi_asset_later_leg_failure_rolls_back_prior_leg(self):
        first = self._leg(self.usd, "multi-fail-usd")
        second = self._leg(self.ngn, "multi-fail-ngn")
        original_record = ledger.record_entry
        calls = 0

        def fail_second_leg(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DatabaseError("second asset leg failed")
            return original_record(*args, **kwargs)

        with patch.object(ledger, "record_entry", side_effect=fail_second_leg):
            with self.assertRaises(DatabaseError):
                ledger.record_multi_asset_entry(
                    "conversion", "multi-fail", [first[1], second[1]]
                )

        self.assertFalse(LedgerEntry.objects.filter(reference_id__startswith="multi-fail").exists())
        self.assertEqual(ledger.ledger_balance(self.user, self.usd), Decimal("0"))
        self.assertEqual(ledger.get_balance(self.user, self.usd).total, Decimal("0"))

    def test_multi_asset_validation_failure_rolls_back_prior_leg(self):
        first = self._leg(self.usd, "multi-invalid-usd")
        invalid = self._leg(self.ngn, "multi-invalid-ngn")[1]
        invalid[1] = ledger.Posting(invalid[1].account, "C", Decimal("9.00"), bucket="available")

        with self.assertRaises(ledger.UnbalancedEntry):
            ledger.record_multi_asset_entry(
                "conversion", "multi-invalid", [first[1], invalid]
            )

        self.assertFalse(LedgerEntry.objects.filter(reference_id="multi-invalid").exists())
        self.assertEqual(ledger.ledger_balance(self.user, self.usd), Decimal("0"))

    def test_multi_asset_success_commits_all_legs(self):
        first = self._leg(self.usd, "multi-ok-usd", Decimal("10.00"))
        second = self._leg(self.ngn, "multi-ok-ngn", Decimal("20.00"))

        entries = ledger.record_multi_asset_entry(
            "conversion", "multi-success", [first[1], second[1]]
        )

        self.assertEqual(len(entries), 2)
        self.assertEqual(ledger.ledger_balance(self.user, self.usd), Decimal("10.00"))
        self.assertEqual(ledger.ledger_balance(self.user, self.ngn), Decimal("20.00"))
        self.assertEqual(ledger.get_balance(self.user, self.usd).available, Decimal("10.00"))
        self.assertEqual(ledger.get_balance(self.user, self.ngn).available, Decimal("20.00"))
