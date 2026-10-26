from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from veltrix_app.models import (
    ChainScanCursor,
    ChainTransfer,
    ReconciliationDiscrepancy,
    ReconciliationRun,
    ScheduledJob,
)
from veltrix_app.services.tron import TronConfigurationError, require_tron_chain_configuration


class Command(BaseCommand):
    help = "Print read-only direct-TRON monitor and reconciliation status."

    def add_arguments(self, parser):
        parser.add_argument(
            "--check",
            action="store_true",
            help="Exit non-zero when a stale cursor, failed/stopped job, discrepancy, or unallocated transfer is found.",
        )

    def handle(self, *args, **options):
        now = timezone.now()
        issues = []
        cursor_stale_seconds = max(1, int(getattr(settings, "TRON_MONITOR_STALE_SECONDS", 900)))
        reconcile_stale_seconds = max(1, int(getattr(settings, "TRON_RECONCILE_STALE_SECONDS", 93600)))
        unallocated_minutes = max(0, int(getattr(settings, "TRON_UNALLOCATED_ALERT_MINUTES", 60)))
        failure_threshold = max(1, int(getattr(settings, "TRON_FAILURE_ALERT_THRESHOLD", 3)))

        def issue(level, message):
            issues.append((level, message))
            self.stdout.write(f"{level.upper()}: {message}")

        cursors = list(ChainScanCursor.objects.select_related("network").order_by("network_id"))
        if settings.TRON_DEPOSITS_ENABLED:
            try:
                require_tron_chain_configuration(live=False)
            except TronConfigurationError:
                issue("error", "TRON monitoring is enabled but chain configuration is invalid.")
            if not cursors:
                issue("error", "TRON monitoring is enabled but no scan cursor exists.")
        if not cursors and not settings.TRON_DEPOSITS_ENABLED:
            self.stdout.write("No TRON scan cursor exists yet.")
        for cursor in cursors:
            self.stdout.write(
                "network={network} cursor_timestamp={timestamp} block={block} "
                "fingerprint={fingerprint} window_end={window_end} updated={updated}".format(
                    network=cursor.network_id,
                    timestamp=cursor.timestamp,
                    block=cursor.block_number,
                    fingerprint=bool(cursor.fingerprint),
                    window_end=cursor.window_end_timestamp or "-",
                    updated=cursor.updated_at.isoformat(),
                )
            )
            if settings.TRON_DEPOSITS_ENABLED:
                age = (now - cursor.updated_at).total_seconds()
                if age > cursor_stale_seconds:
                    issue(
                        "error",
                        f"TRON scan cursor for network={cursor.network_id} is stale by {int(age)} seconds.",
                    )

        for status in ChainTransfer.Status.values:
            count = ChainTransfer.objects.filter(status=status).count()
            self.stdout.write(f"transfers[{status}]={count}")

        scan_job = ScheduledJob.objects.filter(name="scan_tron_deposits").first()
        reconcile_job = ScheduledJob.objects.filter(name="reconcile_deposits").first()
        for job in (scan_job, reconcile_job):
            if job:
                self.stdout.write(
                    "job={name} last_started={started} last_success={success} "
                    "last_failure={failure} consecutive_failures={failures} "
                    "lock_live={lock_live} last_error_present={error}".format(
                        name=job.name,
                        started=job.last_started_at.isoformat() if job.last_started_at else "-",
                        success=job.last_success_at.isoformat() if job.last_success_at else "-",
                        failure=job.last_failure_at.isoformat() if job.last_failure_at else "-",
                        failures=job.consecutive_failures,
                        lock_live=bool(job.lock_owner and job.lock_expires_at and job.lock_expires_at > now),
                        error=bool(job.last_error),
                    )
                )

        if settings.TRON_DEPOSITS_ENABLED:
            if not scan_job:
                issue("error", "No scan_tron_deposits job heartbeat exists.")
            else:
                if scan_job.consecutive_failures >= failure_threshold:
                    issue(
                        "error",
                        f"scan_tron_deposits has failed {scan_job.consecutive_failures} times consecutively.",
                    )
                if not scan_job.last_success_at:
                    issue("error", "scan_tron_deposits has never recorded a successful run.")
                elif (now - scan_job.last_success_at).total_seconds() > cursor_stale_seconds:
                    age = int((now - scan_job.last_success_at).total_seconds())
                    issue("error", f"scan_tron_deposits heartbeat is stale by {age} seconds.")

        if settings.FUNDING_DEPOSITS_ENABLED:
            if not reconcile_job:
                issue("warning", "No reconcile_deposits job heartbeat exists.")
            else:
                if reconcile_job.consecutive_failures >= failure_threshold:
                    issue(
                        "error",
                        f"reconcile_deposits has failed {reconcile_job.consecutive_failures} times consecutively.",
                    )
                if not reconcile_job.last_success_at:
                    issue("warning", "reconcile_deposits has never recorded a successful run.")
                elif (now - reconcile_job.last_success_at).total_seconds() > reconcile_stale_seconds:
                    age = int((now - reconcile_job.last_success_at).total_seconds())
                    issue("error", f"reconcile_deposits heartbeat is stale by {age} seconds.")

        open_discrepancies = ReconciliationDiscrepancy.objects.filter(
            status=ReconciliationDiscrepancy.Status.OPEN
        )
        critical_open = open_discrepancies.filter(severity="critical").count()
        open_count = open_discrepancies.count()
        if open_count:
            self.stdout.write(f"open_discrepancies={open_count} critical={critical_open}")
            issue("warning", f"{open_count} reconciliation discrepancy record(s) remain open.")
        if critical_open:
            issue("error", f"{critical_open} critical reconciliation discrepancy record(s) remain open.")

        if settings.TRON_DEPOSITS_ENABLED and unallocated_minutes:
            cutoff = now - timedelta(minutes=unallocated_minutes)
            unallocated = ChainTransfer.objects.filter(
                deposit__isnull=True,
                observed_at__lt=cutoff,
                status__in=(
                    ChainTransfer.Status.DETECTED,
                    ChainTransfer.Status.CONFIRMED,
                    ChainTransfer.Status.MANUAL_REVIEW,
                ),
            ).count()
            if unallocated:
                issue(
                    "error",
                    f"{unallocated} TRON transfer(s) have remained unallocated for more than "
                    f"{unallocated_minutes} minutes.",
                )

        runs = ReconciliationRun.objects.order_by("-ran_at")[:4]
        if not runs:
            self.stdout.write("No reconciliation run exists yet.")
        for run in runs:
            self.stdout.write(
                f"reconciliation rail={run.rail} status={run.status} "
                f"difference={run.difference} findings={run.discrepancy_count} ran_at={run.ran_at.isoformat()}"
            )

        if options["check"] and any(level == "error" for level, _ in issues):
            raise CommandError(f"Operational check failed with {sum(1 for level, _ in issues if level == 'error')} error(s).")
