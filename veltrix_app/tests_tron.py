from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import time

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from veltrix_app.models import (
    Asset,
    ChainScanCursor,
    ChainTransfer,
    CryptoDeposit,
    Deposit,
    DepositProvider,
    Network,
)
from veltrix_app.services import ledger
from veltrix_app.services.deposits import create_deposit_intent
from veltrix_app.services.tron import (
    MAINNET_USDT,
    TronConfigurationError,
    TronProviderError,
    approve_transfer,
    assign_transfer,
    configured_contract,
    credit_confirmed_transfer,
    get_configured_network,
    parse_transfer_event,
    public_tron_configuration,
    receiving_address,
    record_txid_submission,
    refresh_confirmations,
    require_tron_chain_configuration,
    scan_next_page,
    _request,
)


RECEIVING = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"
SENDER = "T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb"
TESTNET_USDT = SENDER
TXID = "a" * 64
START_TS = 1_600_000_000_000
HEAD_TS = 1_600_003_600_000
WINDOW_START = START_TS - 120_000
WINDOW_END = WINDOW_START + 3_600_000 - 1


def transfer_event(**changes):
    item = {
        "contract_address": MAINNET_USDT,
        "event_name": "Transfer",
        "transaction_id": TXID,
        "event_index": 0,
        "block_number": 42,
        "block_timestamp": 1_600_000_100_000,
        "block_hash": "hash",
        "result": {"from": SENDER, "to": RECEIVING, "value": "1234567"},
    }
    item.update(changes)
    return item


class TronRouter:
    def __init__(self, *, event=None, pages=None, solid_height=50, head_ts=HEAD_TS,
                 block_hash="hash", tx_info=None, tx_events=None, contract=None):
        self.event = event or transfer_event()
        self.pages = list(pages) if pages is not None else None
        self.solid_height = solid_height
        self.head_ts = head_ts
        self.block_hash = block_hash
        self.tx_info = tx_info
        self.tx_events = tx_events
        self.contract = contract or self.event.get("contract_address") or MAINNET_USDT

    def __call__(self, path, params=None):
        if path == "/wallet/getnowblock":
            return {"block_header": {"raw_data": {"number": 1000, "timestamp": self.head_ts}}}
        if path == "/walletsolidity/getnowblock":
            return {"block_header": {"raw_data": {"number": self.solid_height, "timestamp": self.head_ts}}}
        if path == "/walletsolidity/getblockbynum":
            return {"blockID": self.block_hash}
        if path == "/walletsolidity/gettransactioninfobyid":
            if self.tx_info is not None:
                return self.tx_info
            return {
                "id": self.event["transaction_id"],
                "blockNumber": self.event["block_number"],
                "receipt": {"result": "SUCCESS"},
            }
        if path.startswith("/v1/transactions/"):
            return {"success": True, "data": self.tx_events if self.tx_events is not None else [self.event]}
        if path == f"/v1/contracts/{self.contract}/events":
            if self.pages is None:
                return {"success": True, "data": [self.event], "meta": {}}
            return self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        raise AssertionError(f"Unexpected TRON path: {path}")


class TronParserTests(TestCase):
    def test_exact_usdt_base_units(self):
        parsed = parse_transfer_event(transfer_event(), expected_contract=MAINNET_USDT, decimals=6)
        self.assertEqual(parsed["amount_base_units"], Decimal("1234567"))
        self.assertEqual(parsed["amount"], Decimal("1.234567"))
        self.assertEqual(parsed["txid"], TXID)

    def test_rejects_wrong_contract_invalid_address_and_malformed_event(self):
        for item in (
            transfer_event(contract_address="bad"),
            transfer_event(result={"from": "bad", "to": RECEIVING, "value": "1"}),
            {"contract_address": MAINNET_USDT},
            transfer_event(result={"from": SENDER, "to": RECEIVING, "value": "0.5"}),
            transfer_event(result={"from": SENDER, "to": RECEIVING, "value": "0"}),
            transfer_event(block_number=0),
            transfer_event(block_timestamp=None),
        ):
            with self.subTest(item=item):
                with self.assertRaises(ValueError):
                    parse_transfer_event(item, expected_contract=MAINNET_USDT, decimals=6)


BASE_SETTINGS = dict(
    CRYPTO_DEPOSIT_MODE="direct_tron",
    TRON_NETWORK="mainnet",
    TRON_API_BASE_URL="https://api.example.invalid",
    TRON_API_KEY="",
    TRON_USDT_CONTRACT=MAINNET_USDT,
    TRON_USDT_RECEIVING_ADDRESS=RECEIVING,
    TRON_REQUIRED_CONFIRMATIONS=2,
    TRON_DEPOSITS_ENABLED=True,
    TRON_LIVE_VERIFIED=True,
    TRON_SCAN_START_TIMESTAMP=START_TS,
    TRON_SCAN_WINDOW_SECONDS=3600,
    TRON_SCAN_OVERLAP_SECONDS=120,
    TRON_SCAN_LAG_SECONDS=0,
    TRON_UNIQUE_AMOUNT_ENABLED=False,
    TRON_AUTO_MATCH_UNIQUE_AMOUNT=False,
    TRON_AUTO_CREDIT_UNIQUE_AMOUNT=False,
    TRON_REQUEST_TTL_MINUTES=0,
    FUNDING_DEPOSITS_ENABLED=True,
    FUNDING_REQUIRE_KYC=False,
    FUNDING_DEPLOYMENT_IS_PRODUCTION=False,
)


@override_settings(**BASE_SETTINGS)
class TronBaseTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="chain-user", email="chain@example.com", password="secret"
        )
        self.asset = Asset.objects.create(
            code="USDT", display_name="Tether", decimals=6,
            asset_class=Asset.AssetClass.CRYPTO, is_active=True,
        )
        self.network = Network.objects.create(
            asset=self.asset, chain="TRON", token_standard="TRC20",
            contract_address=MAINNET_USDT, decimals=6, required_confirmations=2,
            is_testnet=False, is_active=True,
        )
        self.provider = DepositProvider.objects.create(
            code="direct_tron", display_name="USDT TRC20", rail=DepositProvider.Rail.CRYPTO,
            is_active=True, is_test=False,
        )
        self.provider.supported_assets.add(self.asset)

    def make_deposit(self, amount="1.234567", key="intent-1", **changes):
        settings_changes = {**BASE_SETTINGS, **changes}
        with override_settings(**settings_changes):
            return create_deposit_intent(
                self.user, self.provider, self.asset, Decimal(amount),
                idempotency_key=key, network=self.network,
            )

    def scan(self, router):
        with patch("veltrix_app.services.tron._request", side_effect=router):
            return scan_next_page(self.network)

    def refresh(self, router):
        with patch("veltrix_app.services.tron._request", side_effect=router):
            return refresh_confirmations(self.network)


class TronConfigurationTests(TestCase):
    @override_settings(**{**BASE_SETTINGS, "TRON_USDT_RECEIVING_ADDRESS": ""})
    def test_missing_or_invalid_shared_address_fails_closed(self):
        with self.assertRaises(TronConfigurationError):
            receiving_address()
        with self.assertRaises(TronConfigurationError):
            public_tron_configuration()

    @override_settings(**{**BASE_SETTINGS, "TRON_USDT_RECEIVING_ADDRESS": "not-an-address"})
    def test_invalid_shared_address_fails_closed(self):
        with self.assertRaises(TronConfigurationError):
            require_tron_chain_configuration(live=False)

    @override_settings(**{**BASE_SETTINGS, "TRON_LIVE_VERIFIED": False})
    def test_live_credit_configuration_requires_operational_verification(self):
        with self.assertRaises(TronConfigurationError):
            require_tron_chain_configuration(live=True)

    @override_settings(**{**BASE_SETTINGS, "TRON_USDT_CONTRACT": SENDER})
    def test_mainnet_contract_must_be_the_pinned_usdt_contract(self):
        with self.assertRaises(TronConfigurationError):
            require_tron_chain_configuration()
        self.assertEqual(configured_contract(), SENDER)

    @override_settings(**{
        **BASE_SETTINGS, "TRON_NETWORK": "testnet",
        "TRON_USDT_CONTRACT": TESTNET_USDT, "TRON_LIVE_VERIFIED": False,
    })
    def test_testnet_accepts_only_an_explicit_test_token_contract(self):
        self.assertEqual(configured_contract(), TESTNET_USDT)
        with override_settings(TRON_USDT_CONTRACT=MAINNET_USDT):
            with self.assertRaises(TronConfigurationError):
                configured_contract()
            with self.assertRaises(TronConfigurationError):
                require_tron_chain_configuration()

    @override_settings(**{
        **BASE_SETTINGS, "TRON_NETWORK": "testnet",
        "TRON_USDT_CONTRACT": TESTNET_USDT, "TRON_LIVE_VERIFIED": False,
        "TRON_API_BASE_URL": "https://api.trongrid.io",
    })
    def test_testnet_rejects_the_trongrid_mainnet_endpoint(self):
        with self.assertRaisesMessage(TronConfigurationError, "mainnet endpoint"):
            require_tron_chain_configuration(live=False)
        with self.assertRaisesMessage(TronConfigurationError, "mainnet endpoint"):
            public_tron_configuration()

    @override_settings(**{
        **BASE_SETTINGS, "TRON_NETWORK": "testnet",
        "TRON_USDT_CONTRACT": TESTNET_USDT, "TRON_LIVE_VERIFIED": False,
        "TRON_API_BASE_URL": "https://nile.trongrid.io",
        "FUNDING_DEPLOYMENT_IS_PRODUCTION": True,
    })
    def test_production_never_exposes_testnet_address(self):
        with self.assertRaisesMessage(TronConfigurationError, "production deployments"):
            public_tron_configuration()
        with self.assertRaisesMessage(TronConfigurationError, "production deployments"):
            require_tron_chain_configuration(live=False)

    @override_settings(**BASE_SETTINGS)
    def test_mainnet_rejects_trongrid_testnet_endpoints(self):
        for endpoint in ("https://nile.trongrid.io", "https://api.shasta.trongrid.io"):
            with self.subTest(endpoint=endpoint), override_settings(TRON_API_BASE_URL=endpoint):
                with self.assertRaisesMessage(TronConfigurationError, "testnet endpoint"):
                    require_tron_chain_configuration(live=False)

    @override_settings(**BASE_SETTINGS)
    def test_matching_known_tron_grid_endpoints_are_accepted(self):
        with override_settings(TRON_API_BASE_URL="https://api.trongrid.io"):
            require_tron_chain_configuration(live=False)
        testnet = {
            **BASE_SETTINGS, "TRON_NETWORK": "testnet",
            "TRON_USDT_CONTRACT": TESTNET_USDT, "TRON_LIVE_VERIFIED": False,
        }
        for endpoint in ("https://nile.trongrid.io", "https://api.shasta.trongrid.io"):
            with self.subTest(endpoint=endpoint), override_settings(
                **{**testnet, "TRON_API_BASE_URL": endpoint}
            ):
                require_tron_chain_configuration(live=False)

    @override_settings(**{
        **BASE_SETTINGS, "TRON_NETWORK": "testnet",
        "TRON_USDT_CONTRACT": TESTNET_USDT, "TRON_LIVE_VERIFIED": False,
        "TRON_API_BASE_URL": "https://api.trongrid.io",
    })
    def test_request_refuses_cross_network_endpoint_before_network_io(self):
        with patch("veltrix_app.services.tron.requests.get") as get, \
                patch("veltrix_app.services.tron.requests.post") as post:
            with self.assertRaises(TronConfigurationError):
                _request("/v1/contracts/test/events")
        get.assert_not_called()
        post.assert_not_called()


@override_settings(**BASE_SETTINGS)
class TronScanAndAttributionTests(TronBaseTests):
    def test_only_shared_address_events_are_persisted(self):
        router = TronRouter(event=transfer_event(result={"from": SENDER, "to": SENDER, "value": "1234567"}))
        self.assertEqual(self.scan(router), 0)
        self.assertEqual(ChainTransfer.objects.count(), 0)
        cursor = ChainScanCursor.objects.get(network=self.network)
        self.assertEqual(cursor.timestamp, WINDOW_END + 1)

    def test_unsuccessful_or_malformed_provider_page_never_advances_cursor(self):
        for page in (
            {"success": False, "data": [], "meta": {}},
            {"success": True, "data": "not-a-list", "meta": {}},
        ):
            router = TronRouter(pages=[page])
            with self.assertRaises(TronProviderError):
                self.scan(router)
            cursor = ChainScanCursor.objects.get(network=self.network)
            self.assertEqual(cursor.timestamp, START_TS)
            self.assertEqual(cursor.fingerprint, "")
        self.assertEqual(ChainTransfer.objects.count(), 0)

    def test_scan_deduplicates_and_advances_fingerprint(self):
        router = TronRouter(pages=[
            {"data": [transfer_event()], "meta": {"fingerprint": "next-page-token"}},
            {"data": [transfer_event()], "meta": {}},
        ])
        self.assertEqual(self.scan(router), 1)
        self.assertEqual(self.scan(router), 0)
        cursor = ChainScanCursor.objects.get(network=self.network)
        self.assertEqual(cursor.block_number, 42)
        self.assertEqual(cursor.fingerprint, "")
        self.assertEqual(cursor.timestamp, WINDOW_END + 1)
        self.assertEqual(ChainTransfer.objects.count(), 1)

    def test_unallocated_transfer_defaults_to_manual_review_after_confirmation(self):
        deposit = self.make_deposit()
        self.scan(TronRouter())
        transfer = ChainTransfer.objects.get()
        self.assertIsNone(transfer.deposit_id)
        self.assertEqual(deposit.status, Deposit.Status.PENDING)
        self.refresh(TronRouter())
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, ChainTransfer.Status.MANUAL_REVIEW)
        self.assertIn("unambiguously attributed", transfer.review_reason)
        self.assertFalse(Deposit.objects.get(pk=deposit.pk).is_credited)

    def test_unique_amount_auto_match_links_but_does_not_credit_by_default(self):
        deposit = self.make_deposit(
            amount="1.234567", key="unique",
            TRON_UNIQUE_AMOUNT_ENABLED=True, TRON_AUTO_MATCH_UNIQUE_AMOUNT=True,
            TRON_AUTO_CREDIT_UNIQUE_AMOUNT=False,
        )
        deposit.refresh_from_db()
        crypto = deposit.crypto
        self.assertGreater(crypto.payable_amount, Decimal("1.234567"))
        self.assertEqual(crypto.requested_amount, Decimal("1.234567"))
        event = transfer_event(result={
            "from": SENDER, "to": RECEIVING,
            "value": str(int(crypto.payable_amount * Decimal(1_000_000))),
        })
        unique_settings = {
            **BASE_SETTINGS, "TRON_UNIQUE_AMOUNT_ENABLED": True,
            "TRON_AUTO_MATCH_UNIQUE_AMOUNT": True,
            "TRON_AUTO_CREDIT_UNIQUE_AMOUNT": False,
        }
        with override_settings(**unique_settings):
            self.scan(TronRouter(event=event))
            self.refresh(TronRouter(event=event))
        transfer = ChainTransfer.objects.get()
        self.assertEqual(transfer.deposit_id, deposit.pk)
        self.assertIn("auto:", transfer.attribution_reason)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, ChainTransfer.Status.CONFIRMED)
        self.assertEqual(Deposit.objects.get(pk=deposit.pk).status, Deposit.Status.VERIFIED)
        with override_settings(**unique_settings):
            with patch("veltrix_app.services.tron._request", side_effect=TronRouter(event=event)):
                credit_confirmed_transfer(transfer.pk)
        transfer.refresh_from_db()
        self.assertNotEqual(transfer.status, ChainTransfer.Status.CREDITED)
        self.assertFalse(Deposit.objects.get(pk=deposit.pk).is_credited)

    def test_explicit_auto_credit_requires_both_opt_ins(self):
        deposit = self.make_deposit(
            amount="1.234567", key="auto-credit",
            TRON_UNIQUE_AMOUNT_ENABLED=True, TRON_AUTO_MATCH_UNIQUE_AMOUNT=True,
            TRON_AUTO_CREDIT_UNIQUE_AMOUNT=True,
        )
        deposit.refresh_from_db()
        amount = deposit.crypto.payable_amount
        event = transfer_event(result={
            "from": SENDER, "to": RECEIVING,
            "value": str(int(amount * Decimal(1_000_000))),
        })
        auto_settings = {**BASE_SETTINGS,
                         "TRON_UNIQUE_AMOUNT_ENABLED": True,
                         "TRON_AUTO_MATCH_UNIQUE_AMOUNT": True,
                         "TRON_AUTO_CREDIT_UNIQUE_AMOUNT": True}
        with override_settings(**auto_settings):
            self.scan(TronRouter(event=event))
            self.refresh(TronRouter(event=event))
        transfer = ChainTransfer.objects.get()
        with override_settings(**auto_settings):
            with patch("veltrix_app.services.tron._request", side_effect=TronRouter(event=event)):
                credit_confirmed_transfer(transfer.pk)
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, ChainTransfer.Status.CREDITED)
        deposit.refresh_from_db()
        self.assertTrue(deposit.is_credited)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), amount)

    def test_admin_assign_and_approve_credits_once_after_canonical_checks(self):
        deposit = self.make_deposit()
        self.scan(TronRouter())
        self.refresh(TronRouter())
        transfer = ChainTransfer.objects.get()
        admin = get_user_model().objects.create_user(
            username="admin", email="admin@example.com", password="secret", role="admin"
        )
        router = TronRouter()
        with patch("veltrix_app.services.tron._request", side_effect=router):
            assign_transfer(transfer.pk, deposit.pk, actor=admin, reason="Verified receipt")
            approved = approve_transfer(transfer.pk, actor=admin, reason="Independent review")
            again = approve_transfer(transfer.pk, actor=admin, reason="Duplicate retry")
        self.assertEqual(approved.status, ChainTransfer.Status.CREDITED)
        self.assertEqual(again.status, ChainTransfer.Status.CREDITED)
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("1.234567"))
        self.assertEqual(
            Deposit.objects.get(pk=deposit.pk).ledger_entry.postings.filter(entry__reference_id=str(deposit.pk)).count(),
            2,
        )

    def test_amount_mismatch_is_flagged_and_never_credited(self):
        deposit = self.make_deposit()
        event = transfer_event(result={"from": SENDER, "to": RECEIVING, "value": "999999"})
        self.scan(TronRouter(event=event))
        transfer = ChainTransfer.objects.get()
        admin = get_user_model().objects.create_user(
            username="admin2", email="admin2@example.com", password="secret", role="admin"
        )
        self.refresh(TronRouter(event=event))
        with patch("veltrix_app.services.tron._request", side_effect=TronRouter(event=event)):
            assign_transfer(transfer.pk, deposit.pk, actor=admin, reason="Mismatch check")
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, ChainTransfer.Status.MANUAL_REVIEW)
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Deposit.Status.MANUAL_REVIEW)
        self.assertFalse(deposit.is_credited)

    def test_reorg_flags_transfer_and_does_not_credit(self):
        deposit = self.make_deposit()
        buy = Decimal("1.234567")
        event = transfer_event(result={
            "from": SENDER, "to": RECEIVING, "value": str(int(buy * Decimal(1_000_000))),
        })
        self.scan(TronRouter(event=event))
        self.refresh(TronRouter(event=event))
        transfer = ChainTransfer.objects.get()
        # Admin link is allowed; approval must fail when the canonical block hash moved.
        admin = get_user_model().objects.create_user(
            username="admin3", email="admin3@example.com", password="secret", role="admin"
        )
        with patch("veltrix_app.services.tron._request", side_effect=TronRouter(event=event)):
            assign_transfer(transfer.pk, deposit.pk, actor=admin, reason="Link for review")
        with patch("veltrix_app.services.tron._request", side_effect=TronRouter(event=event, block_hash="changed")):
            with self.assertRaises(TronConfigurationError):
                approve_transfer(transfer.pk, actor=admin, reason="Hash changed")
        deposit.refresh_from_db()
        self.assertFalse(deposit.is_credited)

    def test_txid_submission_is_only_a_claim(self):
        deposit = self.make_deposit()
        outcome = record_txid_submission(deposit, TXID, actor=self.user)
        self.assertEqual(outcome["status"], "recorded")
        deposit.refresh_from_db()
        self.assertEqual(deposit.crypto.submitted_txid, TXID)
        self.assertEqual(deposit.status, Deposit.Status.PENDING)
        self.assertFalse(deposit.is_credited)
        with self.assertRaises(Exception):
            record_txid_submission(deposit, "b" * 64, actor=self.user)
        with self.assertRaises(Exception):
            record_txid_submission(deposit, "not-a-hash", actor=self.user)

    def test_claim_for_transfer_already_linked_to_another_deposit_is_rejected(self):
        first = self.make_deposit(key="first")
        second = self.make_deposit(key="second")
        ChainTransfer.objects.create(
            network=self.network, txid=TXID, event_index=0, block_number=42,
            contract_address=MAINNET_USDT, sender_address=SENDER,
            recipient_address=RECEIVING, amount_base_units=Decimal("1234567"),
            amount=Decimal("1.234567"), deposit=first,
        )
        with self.assertRaises(Exception):
            record_txid_submission(second, TXID, actor=self.user)

    def test_unique_amount_retry_with_same_idempotency_key_is_idempotent(self):
        settings_changes = {"TRON_UNIQUE_AMOUNT_ENABLED": True}
        first = self.make_deposit(key="unique-retry", **settings_changes)
        payable = first.crypto.payable_amount
        self.assertGreater(payable, Decimal("1.234567"))
        retry = self.make_deposit(key="unique-retry", **settings_changes)
        self.assertEqual(retry.pk, first.pk)
        self.assertEqual(retry.crypto.requested_amount, Decimal("1.234567"))
        self.assertEqual(retry.crypto.payable_amount, payable)
        with self.assertRaises(Exception):
            self.make_deposit(amount="2.000000", key="unique-retry", **settings_changes)

    def test_unique_payable_suffix_is_not_reused_after_expiry(self):
        unique = {"TRON_UNIQUE_AMOUNT_ENABLED": True}
        first = self.make_deposit(key="expired-unique", **unique)
        first_suffix = first.crypto.unique_amount_suffix
        self.assertGreater(first_suffix, 0)
        first.crypto.expires_at = timezone.now() - timedelta(minutes=1)
        first.crypto.save(update_fields=["expires_at"])
        second = self.make_deposit(key="new-unique", **unique)
        self.assertNotEqual(second.crypto.unique_amount_suffix, first_suffix)
        self.assertNotEqual(second.crypto.payable_amount, first.crypto.payable_amount)

    def test_legacy_zero_suffix_deposit_is_never_auto_linked(self):
        legacy = self.make_deposit(key="legacy-zero-suffix")
        self.assertEqual(legacy.crypto.unique_amount_suffix, 0)
        amount = legacy.expected_amount
        event = transfer_event(result={
            "from": SENDER, "to": RECEIVING,
            "value": str(int(amount * Decimal(1_000_000))),
        })
        matching = {
            **BASE_SETTINGS, "TRON_UNIQUE_AMOUNT_ENABLED": True,
            "TRON_AUTO_MATCH_UNIQUE_AMOUNT": True,
        }
        with override_settings(**matching):
            self.scan(TronRouter(event=event))
        transfer = ChainTransfer.objects.get()
        self.assertIsNone(transfer.deposit_id)
        self.assertEqual(Deposit.objects.get(pk=legacy.pk).status, Deposit.Status.PENDING)

    def test_one_deposit_cannot_be_linked_to_two_chain_transfers(self):
        deposit = self.make_deposit()
        admin = get_user_model().objects.create_user(
            username="link-admin", email="link-admin@example.com", password="secret", role="admin"
        )
        first = ChainTransfer.objects.create(
            network=self.network, txid="b" * 64, event_index=0, block_number=42,
            contract_address=MAINNET_USDT, sender_address=SENDER,
            recipient_address=RECEIVING, amount_base_units=Decimal("1234567"),
            amount=Decimal("1.234567"),
        )
        second = ChainTransfer.objects.create(
            network=self.network, txid="c" * 64, event_index=0, block_number=43,
            contract_address=MAINNET_USDT, sender_address=SENDER,
            recipient_address=RECEIVING, amount_base_units=Decimal("1234567"),
            amount=Decimal("1.234567"),
        )
        assign_transfer(first.pk, deposit.pk, actor=admin, reason="First receipt")
        with self.assertRaises(ValueError):
            assign_transfer(second.pk, deposit.pk, actor=admin, reason="Duplicate receipt")

    def test_global_funding_switch_blocks_manual_chain_credit(self):
        deposit = self.make_deposit()
        self.scan(TronRouter())
        self.refresh(TronRouter())
        transfer = ChainTransfer.objects.get()
        admin = get_user_model().objects.create_user(
            username="gate-admin", email="gate-admin@example.com", password="secret", role="admin"
        )
        with patch("veltrix_app.services.tron._request", side_effect=TronRouter()):
            assign_transfer(transfer.pk, deposit.pk, actor=admin, reason="Verified receipt")
        with override_settings(FUNDING_DEPOSITS_ENABLED=False):
            with patch("veltrix_app.services.tron._request", side_effect=TronRouter()):
                with self.assertRaises(TronConfigurationError):
                    approve_transfer(transfer.pk, actor=admin, reason="Independent review")
        transfer.refresh_from_db()
        deposit.refresh_from_db()
        self.assertNotEqual(transfer.status, ChainTransfer.Status.CREDITED)
        self.assertFalse(deposit.is_credited)

    def test_unconfirmed_transfer_is_not_credited(self):
        deposit = self.make_deposit()
        self.scan(TronRouter(solid_height=42))
        self.refresh(TronRouter(solid_height=42))
        transfer = ChainTransfer.objects.get()
        self.assertEqual(transfer.status, ChainTransfer.Status.DETECTED)
        self.assertFalse(Deposit.objects.get(pk=deposit.pk).is_credited)

    def test_mainnet_detection_without_live_attestation_confirms_but_never_credits(self):
        matching = {
            "TRON_UNIQUE_AMOUNT_ENABLED": True,
            "TRON_AUTO_MATCH_UNIQUE_AMOUNT": True,
        }
        deposit = self.make_deposit(**matching)
        value = str(int(deposit.crypto.payable_amount * Decimal("1000000")))
        event = transfer_event(result={"from": SENDER, "to": RECEIVING, "value": value})
        off = {**BASE_SETTINGS, **matching, "TRON_LIVE_VERIFIED": False}
        with override_settings(**off):
            self.scan(TronRouter(event=event, solid_height=50))
            self.refresh(TronRouter(event=event, solid_height=50))
        transfer = ChainTransfer.objects.get()
        deposit.refresh_from_db()
        self.assertEqual(transfer.status, ChainTransfer.Status.CONFIRMED)
        self.assertEqual(deposit.status, Deposit.Status.VERIFIED)
        self.assertFalse(deposit.is_credited)
        self.assertIn("live crediting is disabled", transfer.review_reason.lower())

    def test_stale_unconfirmed_transfer_is_flagged_and_never_credited(self):
        deposit = self.make_deposit()
        self.scan(TronRouter(solid_height=42))
        transfer = ChainTransfer.objects.get()
        old = timezone.now() - timedelta(minutes=61)
        ChainTransfer.objects.filter(pk=transfer.pk).update(observed_at=old)
        with override_settings(**{**BASE_SETTINGS, "TRON_UNCONFIRMED_REVIEW_MINUTES": 60}):
            self.refresh(TronRouter(solid_height=42))
        transfer.refresh_from_db()
        self.assertEqual(transfer.status, ChainTransfer.Status.MANUAL_REVIEW)
        self.assertIn("confirmations", transfer.review_reason)
        self.assertFalse(Deposit.objects.get(pk=deposit.pk).is_credited)

    def test_failed_on_chain_receipt_is_flagged_and_never_credited(self):
        deposit = self.make_deposit()
        event = transfer_event(result={
            "from": SENDER, "to": RECEIVING, "value": "1234567",
        })
        self.scan(TronRouter(event=event))
        failed_info = {
            "id": TXID, "blockNumber": 42, "receipt": {"result": "OUT_OF_ENERGY"},
        }
        self.refresh(TronRouter(event=event, tx_info=failed_info))
        transfer = ChainTransfer.objects.get()
        self.assertEqual(transfer.status, ChainTransfer.Status.MANUAL_REVIEW)
        self.assertIn("successful", transfer.review_reason.lower())
        self.assertFalse(Deposit.objects.get(pk=deposit.pk).is_credited)

    def test_multiple_token_events_use_the_matching_event_index(self):
        deposit = self.make_deposit()
        first = transfer_event(event_index=0, result={
            "from": SENDER, "to": RECEIVING, "value": "5000000",
        })
        target = transfer_event(event_index=1)
        router = TronRouter(event=target, tx_events=[first, target])
        self.scan(router)
        self.refresh(router)
        transfer = ChainTransfer.objects.get(event_index=1)
        admin = get_user_model().objects.create_user(
            username="events-admin", email="events@example.com", password="secret", role="admin",
        )
        with patch("veltrix_app.services.tron._request", side_effect=router):
            assign_transfer(transfer.pk, deposit.pk, actor=admin, reason="Correct event index")
            approve_transfer(transfer.pk, actor=admin, reason="Independent review")
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("1.234567"))

    def test_admin_can_accept_verified_amount_mismatch_with_reason(self):
        deposit = self.make_deposit()
        event = transfer_event(result={
            "from": SENDER, "to": RECEIVING, "value": "999999",
        })
        self.scan(TronRouter(event=event))
        self.refresh(TronRouter(event=event))
        transfer = ChainTransfer.objects.get()
        admin = get_user_model().objects.create_user(
            username="mismatch-admin", email="mismatch@example.com",
            password="secret", role="admin",
        )
        with patch("veltrix_app.services.tron._request", side_effect=TronRouter(event=event)):
            assign_transfer(transfer.pk, deposit.pk, actor=admin, reason="Verified underpayment")
        deposit.refresh_from_db()
        self.assertEqual(deposit.status, Deposit.Status.MANUAL_REVIEW)
        with patch("veltrix_app.services.tron._request", side_effect=TronRouter(event=event)):
            approved = approve_transfer(
                transfer.pk,
                actor=admin,
                reason="Bank statement and chain both confirm 0.999999 USDT received",
                allow_amount_mismatch=True,
            )
        self.assertEqual(approved.status, ChainTransfer.Status.CREDITED)
        deposit.refresh_from_db()
        self.assertTrue(deposit.is_credited)
        self.assertEqual(deposit.received_amount, Decimal("0.999999"))
        self.assertEqual(ledger.ledger_balance(self.user, self.asset), Decimal("0.999999"))

    def test_staff_administrator_can_assign_and_approve(self):
        deposit = self.make_deposit()
        self.scan(TronRouter())
        self.refresh(TronRouter())
        transfer = ChainTransfer.objects.get()
        staff = get_user_model().objects.create_user(
            username="staff-admin", email="staff@example.com", password="secret",
            is_staff=True,
        )
        with patch("veltrix_app.services.tron._request", side_effect=TronRouter()):
            assign_transfer(transfer.pk, deposit.pk, actor=staff, reason="Staff review")
            approved = approve_transfer(transfer.pk, actor=staff, reason="Staff approval")
        self.assertEqual(approved.status, ChainTransfer.Status.CREDITED)

    def test_conflicting_claims_never_credit_without_attribution(self):
        first = self.make_deposit(key="claim-one")
        second = self.make_deposit(key="claim-two")
        record_txid_submission(first, TXID, actor=self.user)
        record_txid_submission(second, TXID, actor=self.user)
        self.scan(TronRouter())
        self.refresh(TronRouter())
        transfer = ChainTransfer.objects.get()
        self.assertIsNone(transfer.deposit_id)
        self.assertEqual(transfer.status, ChainTransfer.Status.MANUAL_REVIEW)
        self.assertFalse(Deposit.objects.get(pk=first.pk).is_credited)
        self.assertFalse(Deposit.objects.get(pk=second.pk).is_credited)


TESTNET_SETTINGS = {
    **BASE_SETTINGS,
    "TRON_NETWORK": "testnet",
    "TRON_USDT_CONTRACT": TESTNET_USDT,
    "TRON_LIVE_VERIFIED": False,
}


@override_settings(**TESTNET_SETTINGS)
class TronTestnetCreditTests(TestCase):
    def test_testnet_can_complete_a_controlled_credit(self):
        user = get_user_model().objects.create_user(
            username="testnet-user", email="testnet@example.com", password="secret"
        )
        asset = Asset.objects.create(
            code="USDT", display_name="Test USDT", decimals=6,
            asset_class=Asset.AssetClass.CRYPTO, is_active=True,
        )
        network = Network.objects.create(
            asset=asset, chain="TRON", token_standard="TRC20",
            contract_address=TESTNET_USDT, decimals=6, required_confirmations=2,
            is_testnet=True, is_active=True,
        )
        provider = DepositProvider.objects.create(
            code="direct_tron", display_name="Test USDT", rail=DepositProvider.Rail.CRYPTO,
            is_active=True, is_test=True,
        )
        provider.supported_assets.add(asset)
        deposit = create_deposit_intent(
            user, provider, asset, Decimal("1.234567"),
            idempotency_key="testnet-credit", network=network,
        )
        router = TronRouter(
            event=transfer_event(contract_address=TESTNET_USDT),
            contract=TESTNET_USDT,
        )
        with patch("veltrix_app.services.tron._request", side_effect=router):
            scan_next_page(network)
            refresh_confirmations(network)
        transfer = ChainTransfer.objects.get()
        admin = get_user_model().objects.create_user(
            username="testnet-admin", email="testnet-admin@example.com",
            password="secret", role="admin",
        )
        with patch("veltrix_app.services.tron._request", side_effect=router):
            assign_transfer(transfer.pk, deposit.pk, actor=admin, reason="Testnet rehearsal")
            approved = approve_transfer(transfer.pk, actor=admin, reason="Testnet approval")
        self.assertEqual(approved.status, ChainTransfer.Status.CREDITED)
        self.assertEqual(ledger.ledger_balance(user, asset), Decimal("1.234567"))


class TronHTTPTests(TestCase):
    class Response:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self.payload = payload
            self.headers = {}

        def raise_for_status(self):
            if self.status_code >= 400:
                raise requests.HTTPError(response=self)

        def json(self):
            return self.payload

    @override_settings(**BASE_SETTINGS)
    def test_rate_limit_and_server_errors_are_retried(self):
        responses = [self.Response(429, {}), self.Response(503, {}), self.Response(200, {"ok": True})]
        with patch("veltrix_app.services.tron.requests.get", side_effect=responses) as get, \
                patch("time.sleep") as sleep:
            self.assertEqual(_request("/v1/contracts/test/events"), {"ok": True})
        self.assertEqual(get.call_count, 3)
        self.assertEqual(sleep.call_count, 2)

    @override_settings(**{**BASE_SETTINGS, "TRON_HTTP_RETRIES": 1})
    def test_exhausted_provider_failure_is_explicit(self):
        responses = [self.Response(429, {}), self.Response(429, {})]
        with patch("veltrix_app.services.tron.requests.get", side_effect=responses), \
                patch("time.sleep"):
            with self.assertRaises(TronProviderError):
                _request("/v1/contracts/test/events")


@override_settings(**BASE_SETTINGS)
class TronAPITests(TronBaseTests):
    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_config_and_create_return_shared_address_and_no_private_material(self):
        config = self.client.get("/api/deposits/config/")
        self.assertEqual(config.status_code, 200)
        self.assertEqual(config.data["tron"]["address"], RECEIVING)
        self.assertNotIn("private", str(config.data).lower())
        response = self.client.post(
            "/api/deposits/", {"amount": "1.234567"}, format="json",
            HTTP_IDEMPOTENCY_KEY="api-create-1",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["instructions"]["address"], RECEIVING)
        self.assertEqual(response.data["expected_amount"], "1.234567")

    @override_settings(**{
        **BASE_SETTINGS, "FUNDING_DEPOSITS_ENABLED": False, "TRON_DEPOSITS_ENABLED": False,
    })
    def test_config_does_not_expose_wallet_while_funding_is_disabled(self):
        config = self.client.get("/api/deposits/config/")
        self.assertEqual(config.status_code, 200)
        self.assertFalse(config.data["deposits_enabled"])
        self.assertFalse(config.data["tron"]["enabled"])
        self.assertNotIn("address", config.data["tron"])
        crypto = next(item for item in config.data["methods"] if item["type"] == "crypto")
        self.assertFalse(crypto["enabled"])
        self.assertNotIn("address", crypto)

    @override_settings(**{
        **BASE_SETTINGS,
        "FUNDING_DEPOSITS_ENABLED": False,
        "TRON_DEPOSITS_ENABLED": True,
        "TRON_LIVE_VERIFIED": False,
    })
    def test_config_displays_configured_tron_option_without_address_or_creation(self):
        config = self.client.get("/api/deposits/config/")
        self.assertEqual(config.status_code, 200)
        self.assertFalse(config.data["deposits_enabled"])
        self.assertTrue(config.data["tron"]["configured"])
        self.assertTrue(config.data["tron"]["display_enabled"])
        self.assertFalse(config.data["tron"]["enabled"])
        self.assertFalse(config.data["tron"]["requests_enabled"])
        self.assertFalse(config.data["tron"]["live_enabled"])
        self.assertNotIn("address", config.data["tron"])
        self.assertNotIn("contract_address", config.data["tron"])
        self.assertIn("configured", config.data["tron"]["error"].lower())
        crypto = next(item for item in config.data["methods"] if item["type"] == "crypto")
        self.assertTrue(crypto["configured"])
        self.assertTrue(crypto["display_enabled"])
        self.assertFalse(crypto["enabled"])
        self.assertNotIn("address", crypto)
        self.assertNotIn("contract", crypto)
        self.assertNotIn(RECEIVING, str(config.data))

        response = self.client.post(
            "/api/deposits/", {"amount": "1.234567"}, format="json",
            HTTP_IDEMPOTENCY_KEY="display-only-create",
        )
        self.assertEqual(response.status_code, 503)
        self.assertEqual(Deposit.objects.filter(user=self.user).count(), 0)

    @override_settings(**{
        **BASE_SETTINGS,
        "FUNDING_DEPOSITS_ENABLED": False,
        "TRON_DEPOSITS_ENABLED": True,
        "TRON_NETWORK": "",
    })
    def test_invalid_tron_configuration_is_not_displayed_or_exposed(self):
        config = self.client.get("/api/deposits/config/")
        self.assertEqual(config.status_code, 200)
        self.assertFalse(config.data["tron"]["configured"])
        self.assertFalse(config.data["tron"]["display_enabled"])
        self.assertFalse(config.data["tron"]["enabled"])
        self.assertFalse(config.data["tron"]["requests_enabled"])
        self.assertNotIn("address", config.data["tron"])
        self.assertIn("TRON_NETWORK", config.data["tron"]["error"])
        crypto = next(item for item in config.data["methods"] if item["type"] == "crypto")
        self.assertFalse(crypto["enabled"])
        self.assertNotIn("address", crypto)

    def test_direct_mode_never_selects_nowpayments_provider(self):
        DepositProvider.objects.create(
            code="nowpayments", display_name="NOWPayments",
            rail=DepositProvider.Rail.CRYPTO, is_active=True, is_test=False,
        ).supported_assets.add(self.asset)
        config = self.client.get("/api/deposits/config/")
        self.assertEqual(config.data["crypto_mode"], "direct_tron")
        response = self.client.post(
            "/api/deposits/", {"amount": "2.000001"}, format="json",
            HTTP_IDEMPOTENCY_KEY="direct-not-nowpayments",
        )
        self.assertEqual(response.status_code, 201, response.data)
        deposit = Deposit.objects.get(pk=response.data["id"])
        self.assertEqual(deposit.provider.code, "direct_tron")

    def test_bank_deposit_is_not_created_without_bank_configuration(self):
        response = self.client.post(
            "/api/deposits/bank/", {"amount": "10.00"}, format="json",
            HTTP_IDEMPOTENCY_KEY="bank-1",
        )
        self.assertEqual(response.status_code, 503)

    def test_txid_endpoint_does_not_credit(self):
        deposit = self.make_deposit()
        response = self.client.post(
            f"/api/deposits/{deposit.pk}/txid/", {"txid": TXID}, format="json"
        )
        self.assertEqual(response.status_code, 202, response.data)
        self.assertEqual(response.data["status"], "recorded")
        deposit.refresh_from_db()
        self.assertFalse(deposit.is_credited)

    def test_client_cannot_read_another_clients_deposit(self):
        other = get_user_model().objects.create_user(username="other", password="secret")
        deposit = self.make_deposit()
        other_client = APIClient()
        other_client.force_authenticate(other)
        self.assertEqual(other_client.get(f"/api/deposits/{deposit.pk}/").status_code, 404)
