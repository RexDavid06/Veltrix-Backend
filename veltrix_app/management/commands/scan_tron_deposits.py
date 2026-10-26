from django.core.management.base import BaseCommand, CommandError
from django.conf import settings

from veltrix_app.services.jobs import JobLocked, job_lock
from veltrix_app.services.tron import (
    TronConfigurationError, TronProviderError, credit_confirmed_transfer,
    get_configured_network, refresh_confirmations, scan_next_page,
)


class Command(BaseCommand):
    help = "Scan one configured TRON USDT network page and refresh observed transfer confirmations."

    def handle(self, *args, **options):
        if not settings.TRON_DEPOSITS_ENABLED:
            raise CommandError("TRON deposit monitoring is disabled.")
        try:
            with job_lock("scan_tron_deposits"):
                network = get_configured_network(live=False)
                # Credit only events which were already confirmed before this run.
                # Newly confirmed events become visible as Confirmed and are
                # credited on the next scheduled invocation. Testnet uses test
                # funds; mainnet requires the separate live-credit attestation.
                can_credit = (
                    settings.FUNDING_DEPOSITS_ENABLED
                    and (network.is_testnet or settings.TRON_LIVE_VERIFIED)
                )
                if can_credit:
                    confirmed_ids = list(
                        network.chain_transfers.filter(status="confirmed").values_list("pk", flat=True)
                    )
                    for transfer_id in confirmed_ids:
                        credit_confirmed_transfer(transfer_id)
                observed = scan_next_page(network)
                updated = refresh_confirmations(network)
        except JobLocked as exc:
            raise CommandError(str(exc)) from exc
        except (TronConfigurationError, TronProviderError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Page processed; observed={observed}, confirmation updates={updated}."
            )
        )
