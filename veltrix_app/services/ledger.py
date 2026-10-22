"""Double-entry ledger for real client funds.

Design rules (mirrors ``docs/deposits-multi-rail-design.md``):

* Every entry balances: the sum of debit amounts equals the sum of credit
  amounts. A posting amount is always positive; the side is explicit, so a
  liability can be reduced (and driven negative by a reversal) without ever
  storing a signed magnitude.
* An entry belongs to exactly one asset. A cross-asset movement (an FX
  conversion) is written as one balanced entry per asset, sharing a
  ``(reference_type, reference_id)`` pair.
* Entries and postings are append-only. A correction is a *reversing* entry
  that points at the original via ``reverses_entry``.
* ``AssetBalance`` is a cache of the ledger, never the source of truth. The
  authoritative customer balance is always re-derivable from the postings.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Sequence

from django.db import IntegrityError, transaction
from django.db.models import Case, F, Q, Sum, When
from django.utils import timezone

from veltrix_app.models import (
    Asset,
    AssetBalance,
    AuditLog,
    LedgerAccount,
    LedgerEntry,
    LedgerPosting,
    User,
)

ZERO = Decimal("0")

#: Buckets on ``AssetBalance`` that hold money already on the ledger.
LEDGER_BUCKETS = ("available", "reserved")


class LedgerError(Exception):
    """Base class for ledger failures."""


class UnbalancedEntry(LedgerError):
    """Raised when the debits and credits of an entry do not cancel out."""


class UnknownAccount(LedgerError):
    """Raised when a posting references an account that does not exist."""


class BalanceMismatch(LedgerError):
    """Raised when the cached balance disagrees with the ledger."""


class InsufficientReserve(LedgerError):
    """Raised when a release asks for more than the reserved slice holds."""


@dataclass(frozen=True)
class Posting:
    """A single ledger line.

    ``bucket`` only matters for customer accounts. On a credit it says which
    slice of the balance receives the money (``available`` or ``reserved``);
    on a debit it names the slice to take from. When omitted on a debit the
    ledger takes from ``reserved`` first and then ``available``.
    """

    account: LedgerAccount
    side: str
    amount: Decimal
    bucket: str = ""

    def __post_init__(self):
        if self.side not in (LedgerPosting.Side.DEBIT, LedgerPosting.Side.CREDIT):
            raise LedgerError(f"Invalid posting side {self.side!r}")
        if self.amount <= ZERO:
            raise LedgerError("Posting amount must be positive; the side carries the direction.")
        if self.bucket and self.bucket not in LEDGER_BUCKETS:
            raise LedgerError(f"Invalid bucket {self.bucket!r}")


# ---------------------------------------------------------------------------
# Account helpers
# ---------------------------------------------------------------------------

def ensure_account(asset: Asset, code: str, user: User | None = None, normal_side: str | None = None) -> LedgerAccount:
    """Return the chart-of-accounts row, creating it on first use.

    Accounts are cheap, stable rows derived from a fixed code list, so
    creating them lazily keeps deployments from having to run a fixture.
    """
    account, _ = LedgerAccount.objects.get_or_create(
        asset=asset,
        code=code,
        user=user,
        defaults={"normal_side": normal_side or LedgerAccount.NormalSide.CREDIT},
    )
    return account


def treasury_account(asset: Asset) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.TREASURY, normal_side=LedgerAccount.NormalSide.DEBIT)


def customer_account(asset: Asset, user: User) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.CUSTOMER, user=user, normal_side=LedgerAccount.NormalSide.CREDIT)


def deposits_in_transit_account(asset: Asset) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.DEPOSITS_IN_TRANSIT, normal_side=LedgerAccount.NormalSide.DEBIT)


def fx_position_account(asset: Asset) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.FX_POSITION, normal_side=LedgerAccount.NormalSide.DEBIT)


def receivable_account(asset: Asset) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.CUSTOMER_RECEIVABLE, normal_side=LedgerAccount.NormalSide.DEBIT)


def adjustment_clearing_account(asset: Asset) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.ADJUSTMENT_CLEARING, normal_side=LedgerAccount.NormalSide.DEBIT)


def fee_revenue_account(asset: Asset) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.DEPOSIT_FEE_REVENUE, normal_side=LedgerAccount.NormalSide.CREDIT)


def network_fee_account(asset: Asset) -> LedgerAccount:
    return ensure_account(asset, LedgerAccount.NETWORK_FEE_EXPENSE, normal_side=LedgerAccount.NormalSide.DEBIT)


# ---------------------------------------------------------------------------
# Balance derivation
# ---------------------------------------------------------------------------

def ledger_balance(user: User, asset: Asset) -> Decimal:
    """The customer's on-ledger balance, derived from the postings.

    Customer accounts are credit-normal, so a credit increases the balance and
    a debit reduces it. A reversal therefore drives the balance negative when
    the client has already spent the money.
    """
    total = (
        LedgerPosting.objects.filter(
            account__code=LedgerAccount.CUSTOMER,
            account__user=user,
            account__asset=asset,
        )
        .annotate(
            effect=Case(
                When(side=LedgerPosting.Side.CREDIT, then=F("amount")),
                default=-F("amount"),
                output_field=LedgerPosting._meta.get_field("amount"),
            )
        )
        .aggregate(total=Sum("effect"))["total"]
    )
    return total or ZERO


def account_balance(account: LedgerAccount) -> Decimal:
    """Balance of a single account, in the direction of its normal side."""
    total = (
        LedgerPosting.objects.filter(account=account)
        .annotate(
            effect=Case(
                When(side=account.normal_side, then=F("amount")),
                default=-F("amount"),
                output_field=LedgerPosting._meta.get_field("amount"),
            )
        )
        .aggregate(total=Sum("effect"))["total"]
    )
    return total or ZERO


def get_balance(user: User, asset: Asset) -> AssetBalance:
    """The cached balance row, created on first touch."""
    balance, _ = AssetBalance.objects.get_or_create(user=user, asset=asset)
    return balance


def _locked_balance(user: User, asset: Asset) -> AssetBalance:
    """The cached balance row, locked for update.

    The cache is read-modify-written, so two concurrent postings would
    otherwise clobber each other (a lost update). ``SELECT ... FOR UPDATE``
    serialises them on backends that support it; on SQLite the write lock on
    the whole database has the same effect.
    """
    balance = (
        AssetBalance.objects.select_for_update()
        .filter(user=user, asset=asset)
        .first()
    )
    if balance is None:
        balance, _ = AssetBalance.objects.get_or_create(user=user, asset=asset)
        balance = AssetBalance.objects.select_for_update().get(pk=balance.pk)
    return balance


def assert_balance_consistent(user: User, asset: Asset) -> Decimal:
    """Check the cache against the ledger and return the ledger balance.

    Raises ``BalanceMismatch`` when the cached slices have drifted, which is
    the signal to freeze the account and investigate.
    """
    cached = get_balance(user, asset)
    authoritative = ledger_balance(user, asset)
    if cached.available + cached.reserved != authoritative:
        raise BalanceMismatch(
            f"Balance mismatch for user={user.id} asset={asset.code}: "
            f"cache={cached.available + cached.reserved} ledger={authoritative}"
        )
    return authoritative


# ---------------------------------------------------------------------------
# Posting
# ---------------------------------------------------------------------------

def _apply_to_cache(posting: Posting) -> None:
    """Move the cached balance for a customer account.

    Only customer accounts are cached; treasury, revenue and expense accounts
    are read straight from the postings when they are needed.
    """
    account = posting.account
    if not account.is_customer_account or account.user_id is None:
        return

    balance = _locked_balance(account.user, account.asset)
    available = balance.available
    reserved = balance.reserved

    if posting.side == LedgerPosting.Side.CREDIT:
        bucket = posting.bucket or "available"
        if bucket == "reserved":
            reserved += posting.amount
        else:
            available += posting.amount
    else:
        if posting.bucket:
            if posting.bucket == "reserved":
                reserved -= posting.amount
            else:
                available -= posting.amount
        else:
            # Take from the reserved slice first: a card chargeback should
            # unwind the reserve, not the spendable balance.
            take = min(reserved, posting.amount)
            reserved -= take
            available -= posting.amount - take

    balance.available = available
    balance.reserved = reserved
    balance.updated_at = timezone.now()
    balance.save(update_fields=["available", "reserved", "updated_at"])


@transaction.atomic
def record_entry(
    reference_type: str,
    reference_id: str,
    postings: Sequence[Posting],
    *,
    asset: Asset | None = None,
    note: str = "",
    actor: User | None = None,
    reverses_entry: LedgerEntry | None = None,
) -> LedgerEntry:
    """Post a balanced group of postings and return the ledger entry.

    The call is idempotent on ``(reference_type, reference_id, asset)``: a
    repeat call returns the original entry without touching balances again.
    That is what makes the credit path safe to retry from a queue worker.
    """
    if not postings:
        raise LedgerError("An entry needs at least one posting.")

    if asset is None:
        assets = {posting.account.asset_id for posting in postings}
        if len(assets) != 1:
            raise LedgerError("An entry must belong to exactly one asset.")
        asset = postings[0].account.asset

    for posting in postings:
        if posting.account.asset_id != asset.id:
            raise LedgerError(
                f"Posting on {posting.account.code} is in a different asset than the entry."
            )

    debit_total = sum(
        (posting.amount for posting in postings if posting.side == LedgerPosting.Side.DEBIT), ZERO
    )
    credit_total = sum(
        (posting.amount for posting in postings if posting.side == LedgerPosting.Side.CREDIT), ZERO
    )
    if debit_total != credit_total:
        raise UnbalancedEntry(
            f"Entry {reference_type}:{reference_id} does not balance: "
            f"debits={debit_total} credits={credit_total}"
        )

    existing = (
        LedgerEntry.objects.select_for_update()
        .filter(reference_type=reference_type, reference_id=reference_id, asset=asset)
        .first()
    )
    if existing is not None:
        return existing

    try:
        # A nested atomic block gives us a savepoint. Without it, catching the
        # IntegrityError here would leave the surrounding transaction aborted
        # on PostgreSQL ("current transaction is aborted"), and the lookup
        # below would fail instead of returning the winning entry.
        with transaction.atomic():
            entry = LedgerEntry.objects.create(
                reference_type=reference_type,
                reference_id=reference_id,
                asset=asset,
                note=note,
                created_by=actor,
                reverses_entry=reverses_entry,
            )
    except IntegrityError:
        # Another writer won the race; the unique constraint did its job.
        entry = LedgerEntry.objects.get(
            reference_type=reference_type, reference_id=reference_id, asset=asset
        )
        return entry

    LedgerPosting.objects.bulk_create(
        [
            LedgerPosting(
                entry=entry,
                account=posting.account,
                side=posting.side,
                amount=posting.amount,
                bucket=posting.bucket,
            )
            for posting in postings
        ]
    )

    for posting in postings:
        _apply_to_cache(posting)

    AuditLog.objects.create(
        user=postings[0].account.user,
        actor=actor,
        action=f"ledger.entry.{reference_type}",
        details=(
            f"entry={entry.id} reference={reference_type}:{reference_id} asset={asset.code} "
            f"debit={debit_total} credit={credit_total} note={note}"
        ),
    )

    return entry


@transaction.atomic
def record_multi_asset_entry(
    reference_type: str,
    reference_id: str,
    groups: Iterable[Sequence[Posting]],
    *,
    note: str = "",
    actor: User | None = None,
) -> list[LedgerEntry]:
    """Post one balanced entry per asset under a shared reference.

    A conversion from USD into NGN is the canonical case: the USD leg and the
    NGN leg are separately balanced entries that share a reference, so the
    ``FX_POSITION`` account carries the platform's open position in each
    asset without ever forcing a single entry to span two assets.
    """
    # Each record_entry has its own atomic block, but this outer transaction
    # makes all legs and their cached-balance updates commit or roll back as one.
    entries = []
    for postings in groups:
        postings = list(postings)
        if not postings:
            continue
        entries.append(
            record_entry(
                reference_type,
                reference_id,
                postings,
                asset=postings[0].account.asset,
                note=note,
                actor=actor,
            )
        )
    return entries


def reverse_entry(
    original: LedgerEntry,
    reference_id: str,
    *,
    reason: str = "",
    actor: User | None = None,
) -> LedgerEntry:
    """Post the mirror image of an entry.

    The reversal is a new, balanced entry that points at the original, so the
    history of what happened stays intact and auditable.
    """
    postings = []
    for posting in original.postings.select_related("account", "account__asset").all():
        side = (
            LedgerPosting.Side.CREDIT
            if posting.side == LedgerPosting.Side.DEBIT
            else LedgerPosting.Side.DEBIT
        )
        bucket = ""
        if posting.account.is_customer_account and posting.side == LedgerPosting.Side.CREDIT:
            # Reverse against the customer's current balance, taking reserved
            # funds first and then available funds. The original bucket may
            # have moved since credit (for example, after a reserve expired).
            # An empty bucket delegates that safe allocation to _apply_to_cache.
            bucket = ""
        postings.append(Posting(account=posting.account, side=side, amount=posting.amount, bucket=bucket))

    return record_entry(
        reference_type=LedgerEntry.ReferenceType.REVERSAL,
        reference_id=reference_id,
        postings=postings,
        asset=original.asset,
        note=reason or f"Reversal of entry {original.id}",
        actor=actor,
        reverses_entry=original,
    )


def move_reserved_to_available(user: User, asset: Asset, amount: Decimal) -> Decimal:
    """Release exactly ``amount`` from the reserved slice into the spendable one.

    This is a *cache* movement only: the money was already on the ledger when
    the deposit was credited, so promoting it from ``reserved`` to
    ``available`` does not change the total and needs no ledger entry.

    Raises ``InsufficientReserve`` when the reserved slice cannot cover the
    amount. Moving less would silently leave the client's spendable balance
    short of the deposit that is being released.
    """
    if amount <= ZERO:
        return ZERO
    balance = _locked_balance(user, asset)
    if balance.reserved < amount:
        raise InsufficientReserve(
            f"Cannot release {amount} for user={user.id} asset={asset.code}: "
            f"reserved={balance.reserved}"
        )
    balance.reserved -= amount
    balance.available += amount
    balance.updated_at = timezone.now()
    balance.save(update_fields=["available", "reserved", "updated_at"])
    return amount


def reconcile_balances() -> list[tuple[int, str]]:
    """Compare every cached balance against the ledger.

    Returns ``(user_id, message)`` problems. A drift freezes the account and
    writes an audit event atomically. Database failures propagate, so a mismatch
    is never reported as handled unless both actions committed.
    """
    problems: list[tuple[int, str]] = []
    for balance in AssetBalance.objects.select_related("user", "asset").all():
        try:
            assert_balance_consistent(balance.user, balance.asset)
        except BalanceMismatch as exc:
            with transaction.atomic():
                affected_user = User.objects.select_for_update().get(pk=balance.user_id)
                affected_user.funds_frozen = True
                affected_user.save(update_fields=["funds_frozen"])
                AuditLog.objects.create(
                    user=affected_user,
                    action="ledger.balance_mismatch",
                    details=(
                        f"user={balance.user_id} asset={balance.asset.code}; "
                        f"account frozen; {exc}"
                    ),
                )
            problems.append((balance.user_id, str(exc)))
    return problems
