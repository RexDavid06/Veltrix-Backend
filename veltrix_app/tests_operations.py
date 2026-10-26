from datetime import timedelta
from decimal import Decimal
from io import StringIO
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from veltrix_app.models import (
    Asset,
    ChainScanCursor,
    ChainTransfer,
    Network,
    ReconciliationDiscrepancy,
    ScheduledJob,
)
from veltrix_app.services.jobs import JobLocked, job_lock


TRON_SETTINGS = dict(
    CRYPTO_DEPOSIT_MODE="direct_tron",
    TRON_NETWORK="mainnet",
    TRON_API_BASE_URL="https://api.trongrid.io",
    TRON_USDT_CONTRACT="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    TRON_USDT_RECEIVING_ADDRESS="TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t",
    TRON_REQUIRED_CONFIRMATIONS=2,
    TRON_DEPOSITS_ENABLED=True,
    TRON_LIVE_VERIFIED=True,
    TRON_JOB_LOCK_SECONDS=60,
    FUNDING_DEPOSITS_ENABLED=True,
    FUNDING_REQUIRE_KYC=False,
)


@override_settings(**TRON_SETTINGS)
class JobLockTests(TestCase):
    def test_lock_prevents_overlap_and_records_heartbeat(self):
        with self.assertRaises(JobLocked):
            with job_lock("scan_tron_deposits"):
                with job_lock("scan_tron_deposits"):
                    self.fail("overlapping job must not start")
        job = ScheduledJob.objects.get(name="scan_tron_deposits")
        self.assertEqual(job.lock_owner, "")
        self.assertIsNone(job.lock_expires_at)
        self.assertEqual(job.consecutive_failures, 1)
        self.assertIsNotNone(job.last_failure_at)
        self.assertIsNotNone(job.last_started_at)
        with job_lock("scan_tron_deposits"):
            pass
        job.refresh_from_db()
        self.assertEqual(job.consecutive_failures, 0)
        self.assertIsNotNone(job.last_success_at)

    def test_expired_lease_is_recovered(self):
        ScheduledJob.objects.create(
            name="stale-job",
            lock_owner="dead-worker",
            lock_expires_at=timezone.now() - timedelta(seconds=1),
        )
        with job_lock("stale-job"):
            pass
        job = ScheduledJob.objects.get(name="stale-job")
        self.assertEqual(job.lock_owner, "")
        self.assertIsNotNone(job.last_success_at)

    def test_scan_command_refuses_to_overlap_an_active_job(self):
        with job_lock("scan_tron_deposits"):
            with self.assertRaises(CommandError):
                call_command("scan_tron_deposits", stdout=StringIO())


@override_settings(
    **TRON_SETTINGS,
    TRON_MONITOR_STALE_SECONDS=1,
    TRON_RECONCILE_STALE_SECONDS=1,
    TRON_UNALLOCATED_ALERT_MINUTES=1,
    TRON_FAILURE_ALERT_THRESHOLD=1,
)
class MonitorStatusTests(TestCase):
    def setUp(self):
        asset = Asset.objects.create(
            code="USDT", display_name="Tether", decimals=6,
            asset_class=Asset.AssetClass.CRYPTO, is_active=True,
        )
        self.network = Network.objects.create(
            asset=asset, chain="TRON", token_standard="TRC20",
            contract_address=TRON_SETTINGS["TRON_USDT_CONTRACT"], decimals=6,
            required_confirmations=2, is_testnet=False, is_active=True,
        )
        old = timezone.now() - timedelta(hours=2)
        ChainScanCursor.objects.create(
            network=self.network, timestamp=1_600_000_000_000, updated_at=old
        )
        ScheduledJob.objects.create(
            name="scan_tron_deposits",
            last_started_at=old,
            last_success_at=old,
            consecutive_failures=2,
            last_failure_at=old,
        )
        ChainTransfer.objects.create(
            network=self.network, txid="a" * 64, event_index=0, block_number=42,
            contract_address=self.network.contract_address,
            sender_address="T9yD14Nj9j7xAB4dbGeiX9h8unkKHxuWwb",
            recipient_address=TRON_SETTINGS["TRON_USDT_RECEIVING_ADDRESS"],
            amount_base_units=Decimal("1000000"), amount=Decimal("1.000000"),
            status=ChainTransfer.Status.MANUAL_REVIEW, observed_at=old,
        )
        ReconciliationDiscrepancy.objects.create(
            kind="unallocated_transfer", severity="critical",
            fingerprint="status-check-critical", details="test finding",
        )

    def test_check_fails_on_stale_cursor_failures_and_unallocated_funds(self):
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command("tron_monitor_status", "--check", stdout=output)
        rendered = output.getvalue()
        self.assertIn("stale", rendered.lower())
        self.assertIn("consecutively", rendered.lower())
        self.assertIn("unallocated", rendered.lower())
        self.assertIn("critical reconciliation", rendered.lower())

    def test_status_output_does_not_include_configured_api_secrets(self):
        output = StringIO()
        with self.assertRaises(CommandError):
            call_command("tron_monitor_status", "--check", stdout=output)
        rendered = output.getvalue()
        self.assertNotIn("api.trongrid.io", rendered)
        self.assertNotIn("TRON_API_KEY", rendered)
