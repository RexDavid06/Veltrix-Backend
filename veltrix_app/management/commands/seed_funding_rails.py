from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from veltrix_app.models import Asset, DepositProvider, Network
from veltrix_app.services.tron import (
    TronConfigurationError, configured_contract, require_tron_chain_configuration,
)


class Command(BaseCommand):
    help = (
        "Create the configured direct-TRON and manual-bank rail rows. "
        "Nothing becomes active unless --activate-* is supplied."
    )

    def add_arguments(self, parser):
        parser.add_argument("--activate-direct-tron", action="store_true")
        parser.add_argument("--activate-bank", action="store_true")
        parser.add_argument("--skip-direct-tron", action="store_true")
        parser.add_argument("--skip-bank", action="store_true")

    def handle(self, *args, **options):
        if not options["skip_direct_tron"] and settings.CRYPTO_DEPOSIT_MODE == "direct_tron":
            try:
                require_tron_chain_configuration(live=False)
                asset, _ = Asset.objects.update_or_create(
                    code="USDT",
                    defaults={
                        "display_name": "Tether USD",
                        "decimals": 6,
                        "asset_class": Asset.AssetClass.CRYPTO,
                        "is_active": True,
                    },
                )
                network, _ = Network.objects.update_or_create(
                    asset=asset,
                    chain="TRON",
                    token_standard="TRC20",
                    contract_address=configured_contract(),
                    defaults={
                        "decimals": 6,
                        "required_confirmations": max(1, settings.TRON_REQUIRED_CONFIRMATIONS),
                        "is_testnet": settings.TRON_NETWORK == "testnet",
                        "is_active": bool(options["activate_direct_tron"]),
                    },
                )
                provider, _ = DepositProvider.objects.update_or_create(
                    code="direct_tron",
                    defaults={
                        "display_name": "USDT (TRC20)",
                        "rail": DepositProvider.Rail.CRYPTO,
                        "is_test": network.is_testnet,
                        "is_active": bool(options["activate_direct_tron"]),
                    },
                )
                provider.supported_assets.add(asset)
                self.stdout.write(self.style.SUCCESS(
                    f"direct_tron ready (network_active={network.is_active}, provider_active={provider.is_active})."
                ))
            except TronConfigurationError as exc:
                if options["activate_direct_tron"]:
                    raise CommandError(str(exc)) from exc
                self.stdout.write(self.style.WARNING(f"direct_tron not configured: {exc}"))

        if not options["skip_bank"]:
            currency = str(getattr(settings, "BANK_TRANSFER_CURRENCY", "") or "").upper()
            if not currency:
                if options["activate_bank"]:
                    raise CommandError("BANK_TRANSFER_CURRENCY must be configured before activating bank transfers.")
                self.stdout.write(self.style.WARNING("Bank rail skipped: BANK_TRANSFER_CURRENCY is empty."))
            else:
                asset, _ = Asset.objects.update_or_create(
                    code=currency,
                    defaults={
                        "display_name": currency,
                        "decimals": 2,
                        "asset_class": Asset.AssetClass.FIAT,
                        "is_active": True,
                    },
                )
                provider, _ = DepositProvider.objects.update_or_create(
                    code="bank_manual",
                    defaults={
                        "display_name": f"Bank transfer ({currency})",
                        "rail": DepositProvider.Rail.BANK,
                        "is_test": False,
                        "is_active": bool(options["activate_bank"]),
                    },
                )
                provider.supported_assets.add(asset)
                self.stdout.write(self.style.SUCCESS(
                    f"bank_manual ready for {currency} (provider_active={provider.is_active})."
                ))
