from django.core.management.base import BaseCommand, CommandError

from veltrix_app.services.jobs import JobLocked, job_lock
from veltrix_app.services.reconciliation import run_reconciliation


class Command(BaseCommand):
    help = "Record read-only deposit reconciliation runs and discrepancies."

    def handle(self, *args, **options):
        try:
            with job_lock("reconcile_deposits"):
                runs = run_reconciliation()
        except JobLocked as exc:
            raise CommandError(str(exc)) from exc
        for run in runs:
            self.stdout.write(
                f"rail={run.rail} status={run.status} difference={run.difference} "
                f"findings={run.discrepancy_count}"
            )
