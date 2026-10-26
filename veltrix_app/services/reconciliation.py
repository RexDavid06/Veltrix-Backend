"""Repeatable deposit reconciliation. Findings never move client money."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from django.db import transaction
from django.db.models import Count, Sum
from django.utils import timezone

from veltrix_app.models import (
    BankDeposit,
    ChainTransfer,
    Deposit,
    LedgerEntry,
    LedgerPosting,
    ReconciliationDiscrepancy,
    ReconciliationRun,
)
from veltrix_app.services import ledger

ZERO = Decimal("0")
CREDITED_STATES = (Deposit.Status.CREDITED, Deposit.Status.AVAILABLE, Deposit.Status.RESERVED)


def _is_admin_actor(actor) -> bool:
    return bool(
        actor
        and getattr(actor, "is_authenticated", False)
        and getattr(actor, "is_active", False)
        and getattr(actor, "is_admin", lambda: False)()
    )


def _treasury_debits(deposits):
    ids = [str(pk) for pk in deposits.values_list("pk", flat=True) if pk]
    if not ids:
        return ZERO
    total = (
        LedgerPosting.objects.filter(
            entry__reference_type=LedgerEntry.ReferenceType.DEPOSIT,
            entry__reference_id__in=ids,
            account__code="1000",
            side=LedgerPosting.Side.DEBIT,
        ).aggregate(total=Sum("amount"))["total"]
    )
    return total or ZERO


def _record(run, *, kind, fingerprint, details, severity="warning", deposit=None, transfer=None):
    discrepancy, _ = ReconciliationDiscrepancy.objects.get_or_create(
        fingerprint=fingerprint,
        defaults={
            "run": run, "kind": kind, "severity": severity, "details": details,
            "deposit": deposit, "chain_transfer": transfer,
        },
    )
    if discrepancy.status == ReconciliationDiscrepancy.Status.OPEN:
        changed = []
        if discrepancy.run_id != run.id:
            discrepancy.run = run
            changed.append("run")
        if discrepancy.details != details:
            discrepancy.details = details
            changed.append("details")
        if changed:
            discrepancy.updated_at = timezone.now()
            changed.append("updated_at")
            discrepancy.save(update_fields=changed)
    run.discrepancy_count += 1
    return discrepancy


@transaction.atomic
def run_reconciliation(*, actor=None) -> list[ReconciliationRun]:
    """Compare direct-TRON and bank facts with the ledger and record findings."""
    now = timezone.now()
    period_start = now - timedelta(days=30)
    runs = []

    direct = Deposit.objects.filter(rail=Deposit.Rail.CRYPTO, provider__code="direct_tron")
    credited_direct = direct.filter(status__in=CREDITED_STATES)
    transfer_total = ChainTransfer.objects.filter(
        status=ChainTransfer.Status.CREDITED
    ).aggregate(total=Sum("amount"))["total"] or ZERO
    run = ReconciliationRun.objects.create(
        rail=Deposit.Rail.CRYPTO,
        period_start=period_start,
        period_end=now,
        provider_total=transfer_total,
        ledger_total=_treasury_debits(credited_direct),
        discrepancy_count=0,
        status="ok",
        notes="Direct-TRON shared-address reconciliation; read-only findings.",
    )
    runs.append(run)

    for transfer in ChainTransfer.objects.filter(deposit__isnull=True).exclude(
        status__in=(ChainTransfer.Status.DETECTED, ChainTransfer.Status.CONFIRMED)
    ).select_related("network")[:500]:
        _record(
            run, kind="chain_unallocated",
            fingerprint=f"chain-unallocated:{transfer.pk}",
            details=(
                f"Transfer {transfer.txid}:{transfer.event_index} for {transfer.amount} USDT "
                f"has no attributed deposit request (status={transfer.status})."
            ),
            transfer=transfer,
        )

    for transfer in ChainTransfer.objects.filter(status=ChainTransfer.Status.CREDITED).select_related("deposit")[:1000]:
        if transfer.deposit is None:
            _record(
                run, kind="chain_credited_without_deposit",
                fingerprint=f"chain-credited-no-deposit:{transfer.pk}",
                details=f"Transfer {transfer.txid}:{transfer.event_index} is marked credited but has no deposit.",
                severity="critical", transfer=transfer,
            )
        elif not transfer.deposit.is_credited:
            _record(
                run, kind="chain_credited_deposit_not_credited",
                fingerprint=f"chain-credited-deposit-not-credited:{transfer.pk}",
                details=f"Transfer {transfer.pk} is credited but deposit {transfer.deposit_id} is not.",
                severity="critical", deposit=transfer.deposit, transfer=transfer,
            )

    for transfer in ChainTransfer.objects.exclude(deposit__isnull=True).exclude(
        status__in=(ChainTransfer.Status.DETECTED,)
    ).select_related("deposit")[:1000]:
        if transfer.amount != transfer.deposit.expected_amount:
            _record(
                run, kind="chain_amount_mismatch",
                fingerprint=f"chain-amount-mismatch:{transfer.pk}",
                details=(
                    f"Transfer {transfer.txid}:{transfer.event_index} amount {transfer.amount} "
                    f"differs from deposit {transfer.deposit_id} expected {transfer.deposit.expected_amount}."
                ),
                deposit=transfer.deposit, transfer=transfer,
            )

    for deposit in direct.filter(
        status__in=(Deposit.Status.DETECTED, Deposit.Status.VERIFYING, Deposit.Status.VERIFIED)
    )[:1000]:
        if not deposit.chain_transfers.exists():
            _record(
                run, kind="direct_verified_without_transfer",
                fingerprint=f"direct-no-transfer:{deposit.pk}",
                details=f"Direct TRON deposit {deposit.pk} is {deposit.status} but has no chain transfer.",
                deposit=deposit,
            )

    duplicate_refs = (
        LedgerEntry.objects.filter(reference_type=LedgerEntry.ReferenceType.DEPOSIT)
        .values("reference_id").annotate(count=Count("id")).filter(count__gt=1)[:500]
    )
    for row in duplicate_refs:
        deposit = Deposit.objects.filter(pk=int(row["reference_id"])).first() if row["reference_id"].isdigit() else None
        _record(
            run, kind="duplicate_deposit_ledger_entries",
            fingerprint=f"duplicate-ledger:{row['reference_id']}",
            details=f"Deposit reference {row['reference_id']} has {row['count']} ledger entries.",
            severity="critical", deposit=deposit,
        )

    for deposit in credited_direct.filter(ledger_entry__isnull=True)[:1000]:
        _record(
            run, kind="credited_without_ledger",
            fingerprint=f"credited-no-ledger:{deposit.pk}",
            details=f"Deposit {deposit.pk} is {deposit.status} without a ledger entry.",
            severity="critical", deposit=deposit,
        )

    for user_id, message in ledger.reconcile_balances():
        _record(
            run, kind="balance_cache_mismatch",
            fingerprint=f"balance-mismatch:{user_id}",
            details=message, severity="critical",
        )

    run.provider_total = run.provider_total or ZERO
    run.ledger_total = run.ledger_total or ZERO
    run.difference = run.provider_total - run.ledger_total
    run.status = "ok" if run.discrepancy_count == 0 and run.difference == ZERO else "review"
    run.save(update_fields=["provider_total", "ledger_total", "difference", "discrepancy_count", "status"])

    bank = Deposit.objects.filter(rail=Deposit.Rail.BANK, status__in=CREDITED_STATES)
    bank_total = BankDeposit.objects.filter(
        deposit__in=bank, verified_amount__isnull=False
    ).aggregate(total=Sum("verified_amount"))["total"] or ZERO
    bank_run = ReconciliationRun.objects.create(
        rail=Deposit.Rail.BANK,
        period_start=period_start,
        period_end=now,
        provider_total=bank_total,
        ledger_total=_treasury_debits(bank),
        discrepancy_count=0,
        status="ok",
        notes="Manual bank reconciliation; provider total is the operator-verified amount.",
    )
    runs.append(bank_run)
    for deposit in bank.filter(ledger_entry__isnull=True)[:1000]:
        _record(
            bank_run, kind="credited_without_ledger",
            fingerprint=f"bank-credited-no-ledger:{deposit.pk}",
            details=f"Bank deposit {deposit.pk} is {deposit.status} without a ledger entry.",
            severity="critical", deposit=deposit,
        )
    for bank_deposit in BankDeposit.objects.filter(deposit__in=bank).select_related("deposit")[:1000]:
        if bank_deposit.verified_amount is None or bank_deposit.verified_amount <= ZERO:
            _record(
                bank_run, kind="bank_credited_without_verified_amount",
                fingerprint=f"bank-no-verified-amount:{bank_deposit.pk}",
                details=f"Bank deposit {bank_deposit.deposit_id} is credited without a positive verified amount.",
                severity="critical", deposit=bank_deposit.deposit,
            )
        elif (
            bank_deposit.verified_currency.upper() != bank_deposit.deposit.asset.code.upper()
            or not bank_deposit.verified_reference
            or not bank_deposit.verification_source
        ):
            _record(
                bank_run, kind="bank_verification_incomplete",
                fingerprint=f"bank-incomplete:{bank_deposit.pk}",
                details=f"Bank deposit {bank_deposit.deposit_id} has incomplete verification evidence.",
                deposit=bank_deposit.deposit,
            )
    bank_run.provider_total = bank_run.provider_total or ZERO
    bank_run.ledger_total = bank_run.ledger_total or ZERO
    bank_run.difference = bank_run.provider_total - bank_run.ledger_total
    bank_run.status = "ok" if bank_run.discrepancy_count == 0 and bank_run.difference == ZERO else "review"
    bank_run.save(update_fields=["provider_total", "ledger_total", "difference", "discrepancy_count", "status"])
    return runs


@transaction.atomic
def resolve_discrepancy(discrepancy_id: int, *, actor, resolution: str, ignore=False):
    if not _is_admin_actor(actor):
        raise ValueError("An administrator actor is required.")
    resolution = str(resolution or "").strip()
    if not resolution:
        raise ValueError("A resolution note is required.")
    discrepancy = ReconciliationDiscrepancy.objects.select_for_update().get(pk=discrepancy_id)
    if discrepancy.status != ReconciliationDiscrepancy.Status.OPEN:
        raise ValueError("Discrepancy is already resolved or ignored; its audit history is immutable.")
    discrepancy.status = (
        ReconciliationDiscrepancy.Status.IGNORED if ignore
        else ReconciliationDiscrepancy.Status.RESOLVED
    )
    discrepancy.resolution = resolution
    discrepancy.resolved_by = actor
    discrepancy.resolved_at = timezone.now()
    discrepancy.updated_at = timezone.now()
    discrepancy.save(update_fields=[
        "status", "resolution", "resolved_by", "resolved_at", "updated_at",
    ])
    return discrepancy
