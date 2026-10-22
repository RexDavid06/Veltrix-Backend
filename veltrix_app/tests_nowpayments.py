import hashlib
import hmac
import json
from decimal import Decimal
from unittest.mock import patch
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework.test import APIClient
from rest_framework.throttling import ScopedRateThrottle, UserRateThrottle

from veltrix_app.models import Asset, CryptoDeposit, Deposit, DepositProvider, KycProfile, Network, ProviderEvent
from veltrix_app.api.deposits import DepositListCreateAPIView
from veltrix_app.services.kyc import approve_profile, ensure_profile
from veltrix_app.services import ledger


User = get_user_model()


@override_settings(FUNDING_DEPOSITS_ENABLED=True, NOWPAYMENTS_ENABLED=True, FUNDING_REQUIRE_KYC=True)
class NowPaymentsCallbackTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="payer@example.com", email="payer@example.com", password="secret")
        self.other = User.objects.create_user(username="other@example.com", email="other@example.com", password="secret")
        approve_profile(ensure_profile(self.user))
        approve_profile(ensure_profile(self.other))
        self.asset = Asset.objects.create(code="USDT", display_name="Tether", decimals=6, asset_class=Asset.AssetClass.CRYPTO)
        self.network = Network.objects.create(asset=self.asset, chain="TRON", token_standard="TRC20", decimals=6,
            required_confirmations=1, is_testnet=False, is_active=True)
        self.provider = DepositProvider.objects.create(code="nowpayments", display_name="NOWPayments",
            rail=DepositProvider.Rail.CRYPTO, is_active=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.deposit = Deposit.objects.create(user=self.user, rail=Deposit.Rail.CRYPTO, asset=self.asset,
            provider=self.provider, expected_amount=Decimal("10"), received_amount=None,
            idempotency_key="test-1", provider_ref="pay-1", reference="VELT-ORDER-1", instructions={
                "provider": "NOWPayments", "network": "TRON (TRC20)", "asset": "USDT",
                "address": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb", "amount": "10", "currency": "USDT"})
        CryptoDeposit.objects.create(deposit=self.deposit, network=self.network, address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb", required_confirmations=1)
        self.payload = {"payment_id": "pay-1", "payment_status": "finished", "order_id": "VELT-ORDER-1"}
        self.provider_response = {"payment_id": "pay-1", "payment_status": "finished", "order_id": "VELT-ORDER-1",
            "pay_currency": "usdttrc20", "pay_address": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb", "actually_paid": "10",
            "payin_hash": "tx-abc", "outcome_amount": "10", "outcome_currency": "usdttrc20"}

    def signed_headers(self, body):
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        signature = hmac.new(b"test-ipn-secret", canonical, hashlib.sha512).hexdigest()
        return {"HTTP_X_NOWPAYMENTS_SIG": signature}

    def post_event(self, body=None, response=None, client=None):
        response = response or self.provider_response
        body = dict(body or self.payload)
        # Model distinct NOWPayments callbacks when the provider status payload differs.
        for key in ("payment_id", "payment_status", "order_id", "pay_currency", "pay_address", "actually_paid"):
            if key in response:
                body[key] = response[key]
        with patch.dict("os.environ", {"NOWPAYMENTS_API_KEY": "test-key", "NOWPAYMENTS_IPN_SECRET": "test-ipn-secret", "NOWPAYMENTS_PAYOUT_WALLET": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"}), \
             patch("veltrix_app.services.adapters.requests.get") as provider_get:
            provider_get.return_value.status_code = 200
            provider_get.return_value.json.return_value = response
            provider_get.return_value.raise_for_status.return_value = None
            return (client or self.client).post(reverse("api_nowpayments_webhook"), data=json.dumps(body),
                content_type="application/json", **self.signed_headers(body))

    def test_verified_payment_credits_existing_ledger_once(self):
        response = self.post_event()
        self.assertEqual(response.status_code, 200)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, Deposit.Status.AVAILABLE)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("10"))
        self.assertEqual(self.deposit.crypto.txid, "tx-abc")
        self.assertEqual(ProviderEvent.objects.get().status, ProviderEvent.Status.PROCESSED)

    def test_fee_paid_by_customer_keeps_requested_owner_credit_exact(self):
        self.deposit.instructions["amount"] = "10.3"
        self.deposit.save(update_fields=["instructions"])
        response = self.post_event(response=dict(self.provider_response, actually_paid="10.3", pay_amount="10.3"))
        self.assertEqual(response.status_code, 200)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.source_amount, Decimal("10.3"))
        self.assertEqual(self.deposit.received_amount, Decimal("10"))
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("10"))

    def test_exact_replay_does_not_credit_twice(self):
        self.post_event()
        self.post_event()
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("10"))
        self.assertEqual(ProviderEvent.objects.count(), 1)

    def test_forged_signature_is_rejected_and_never_credits(self):
        response = self.client.post(reverse("api_nowpayments_webhook"), data=json.dumps(self.payload), content_type="application/json",
            HTTP_X_NOWPAYMENTS_SIG="bad")
        self.assertEqual(response.status_code, 400)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, Deposit.Status.PENDING)
        self.assertEqual(ProviderEvent.objects.get().signature_valid, False)

    def test_wrong_token_network_or_recipient_goes_to_manual_review(self):
        for field, value in (("pay_currency", "usdt"), ("pay_address", "TOther"), ("payment_id", "other"), ("outcome_currency", "usdt")):
            response = dict(self.provider_response, **{field: value})
            self.post_event(response=response)
            self.deposit.refresh_from_db()
            self.assertEqual(self.deposit.status, Deposit.Status.MANUAL_REVIEW)
            self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))
            # reset this isolated intent for the next mismatched fact
            self.deposit.status = Deposit.Status.PENDING
            self.deposit.review_reason = ""
            self.deposit.save(update_fields=["status", "review_reason"])

    def test_underpayment_and_overpayment_are_reviewed(self):
        for amount in ("9", "11"):
            self.post_event(response=dict(self.provider_response, actually_paid=amount))
            self.deposit.refresh_from_db()
            self.assertEqual(self.deposit.status, Deposit.Status.MANUAL_REVIEW)
            self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))
            self.deposit.status = Deposit.Status.PENDING
            self.deposit.review_reason = ""
            self.deposit.save(update_fields=["status", "review_reason"])

    def test_late_payment_after_expiry_is_reviewed(self):
        self.deposit.status = Deposit.Status.EXPIRED
        self.deposit.save(update_fields=["status"])
        response = self.post_event()
        self.assertEqual(response.status_code, 200, ProviderEvent.objects.get().error)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, Deposit.Status.MANUAL_REVIEW)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))

    def test_cross_user_deposit_details_are_hidden(self):
        self.client.force_authenticate(self.other)
        response = self.client.get(reverse("api_deposit_detail", args=[self.deposit.pk]))
        self.assertEqual(response.status_code, 404)

    @patch.dict("os.environ", {"NOWPAYMENTS_API_KEY": "test-key", "NOWPAYMENTS_IPN_SECRET": "test-ipn-secret", "NOWPAYMENTS_PAYOUT_WALLET": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"})
    @patch("veltrix_app.services.adapters.requests.get")
    def test_provider_timeout_does_not_credit_and_same_event_can_retry(self, provider_get):
        import requests
        provider_get.side_effect = requests.Timeout("timeout")
        body = self.payload
        headers = self.signed_headers(body)
        url = reverse("api_nowpayments_webhook")
        response = self.client.post(url, data=json.dumps(body), content_type="application/json", **headers)
        self.assertEqual(response.status_code, 503)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, Deposit.Status.PENDING)
        provider_get.side_effect = None
        provider_get.return_value.status_code = 200
        provider_get.return_value.json.return_value = self.provider_response
        provider_get.return_value.raise_for_status.return_value = None
        retry = self.client.post(url, data=json.dumps(body), content_type="application/json", **headers)
        self.assertEqual(retry.status_code, 200)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("10"))

    @patch.dict("os.environ", {"NOWPAYMENTS_API_KEY": "test-key", "NOWPAYMENTS_IPN_SECRET": "test-ipn-secret", "NOWPAYMENTS_PAYOUT_WALLET": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"})
    @patch("veltrix_app.services.adapters.requests.get")
    @patch("veltrix_app.services.ledger.record_entry", side_effect=RuntimeError("ledger unavailable"))
    def test_ledger_failure_rolls_back_and_retries_safely(self, record, provider_get):
        provider_get.return_value.status_code = 200
        provider_get.return_value.json.return_value = self.provider_response
        provider_get.return_value.raise_for_status.return_value = None
        response = self.client.post(reverse("api_nowpayments_webhook"), data=json.dumps(self.payload),
            content_type="application/json", **self.signed_headers(self.payload))
        self.assertEqual(response.status_code, 503)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, Deposit.Status.PENDING)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))
        self.assertEqual(ProviderEvent.objects.get().status, ProviderEvent.Status.FAILED)

    def test_confirming_status_is_visible_without_credit(self):
        response = self.post_event(response=dict(self.provider_response, payment_status="confirming"))
        self.assertEqual(response.status_code, 200)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, Deposit.Status.DETECTED)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))

    def test_sandbox_success_never_credits_real_ledger(self):
        with patch.dict("os.environ", {"NOWPAYMENTS_SANDBOX": "1"}):
            response = self.post_event()
        self.assertEqual(response.status_code, 200)
        self.deposit.refresh_from_db()
        self.assertEqual(self.deposit.status, Deposit.Status.MANUAL_REVIEW)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0"))


@override_settings(FUNDING_DEPOSITS_ENABLED=True, NOWPAYMENTS_ENABLED=True, FUNDING_REQUIRE_KYC=False)
class NowPaymentsIntentTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="intent@example.com", email="intent@example.com", password="secret")
        self.asset = Asset.objects.create(code="USDT", display_name="Tether", decimals=6, asset_class=Asset.AssetClass.CRYPTO)
        self.network = Network.objects.create(asset=self.asset, chain="TRON", token_standard="TRC20", decimals=6,
            is_testnet=False, is_active=True)
        self.provider = DepositProvider.objects.create(code="nowpayments", display_name="NOWPayments",
            rail=DepositProvider.Rail.CRYPTO, is_active=True)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_admin_cannot_list_or_create_client_deposits(self):
        admin = User.objects.create_user(
            username="funding-admin@example.com", email="funding-admin@example.com",
            password="secret", role="admin", is_staff=True,
        )
        admin_client = APIClient()
        admin_client.force_authenticate(admin)

        self.assertEqual(admin_client.get(reverse("api_deposits")).status_code, 403)
        response = admin_client.post(
            reverse("api_deposits"),
            {"amount": "15", "idempotency_key": "admin-must-not-create"},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(Deposit.objects.count(), 0)

    @patch.dict("os.environ", {"NOWPAYMENTS_API_KEY": "key", "NOWPAYMENTS_IPN_SECRET": "secret",
        "NOWPAYMENTS_IPN_CALLBACK_URL": "https://example.test/api/webhooks/nowpayments/",
        "NOWPAYMENTS_PAYOUT_WALLET": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"})
    @patch("veltrix_app.services.adapters.requests.post")
    def test_customer_receives_provider_address_and_network_instructions(self, provider_post):
        provider_post.return_value.status_code = 201
        provider_post.return_value.json.return_value = {"payment_id": "np-10", "pay_address": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
            "pay_amount": "15", "pay_currency": "usdttrc20"}
        provider_post.return_value.raise_for_status.return_value = None
        response = self.client.post(reverse("api_deposits"), {"amount": "15", "idempotency_key": "my-deposit-1"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["instructions"]["address"], "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb")
        self.assertEqual(response.data["instructions"]["network"], "TRON (TRC20)")
        self.assertEqual(response.data["instructions"]["amount"], "15")
        self.assertNotIn("api_key", response.data)

    @patch.dict("os.environ", {"NOWPAYMENTS_API_KEY": "sandbox-key", "NOWPAYMENTS_IPN_SECRET": "sandbox-secret",
        "NOWPAYMENTS_IPN_CALLBACK_URL": "https://example.test/api/webhooks/nowpayments/",
        "NOWPAYMENTS_PAYOUT_WALLET": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb", "NOWPAYMENTS_SANDBOX": "1"})
    @patch("veltrix_app.services.adapters.requests.post")
    def test_sandbox_creation_targets_sandbox_and_requests_success_case(self, provider_post):
        provider_post.return_value.status_code = 201
        provider_post.return_value.json.return_value = {"payment_id": "np-sandbox", "pay_address": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
            "pay_amount": "15", "pay_currency": "usdttrc20"}
        provider_post.return_value.raise_for_status.return_value = None
        response = self.client.post(reverse("api_deposits"), {"amount": "15", "idempotency_key": "sandbox-case"}, format="json")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(provider_post.call_args.args[0], "https://api-sandbox.nowpayments.io/v1/payment")
        self.assertEqual(provider_post.call_args.kwargs["json"]["case"], "success")

    def test_missing_provider_secrets_never_creates_fake_address(self):
        response = self.client.post(reverse("api_deposits"), {"amount": "15", "idempotency_key": "missing-config"}, format="json")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Deposit.objects.count(), 0)

    def test_history_polling_uses_user_throttle_not_deposit_creation_quota(self):
        view = DepositListCreateAPIView()
        view.request = SimpleNamespace(method="GET")
        self.assertIsInstance(view.get_throttles()[0], UserRateThrottle)
        view.request = SimpleNamespace(method="POST")
        self.assertIsInstance(view.get_throttles()[0], ScopedRateThrottle)
        self.assertEqual(view.throttle_scope, "deposit_create")

    @patch.dict("os.environ", {"NOWPAYMENTS_API_KEY": "key", "NOWPAYMENTS_IPN_SECRET": "secret",
        "NOWPAYMENTS_IPN_CALLBACK_URL": "http://insecure.example/api/webhooks/nowpayments/",
        "NOWPAYMENTS_PAYOUT_WALLET": "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"})
    @patch("veltrix_app.services.adapters.requests.post")
    def test_payment_creation_rejects_non_https_callback(self, provider_post):
        response = self.client.post(reverse("api_deposits"), {"amount": "15", "idempotency_key": "insecure-callback"}, format="json")
        self.assertEqual(response.status_code, 503)
        provider_post.assert_not_called()
        self.assertEqual(Deposit.objects.count(), 0)
