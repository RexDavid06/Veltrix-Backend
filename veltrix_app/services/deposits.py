"""The deposit pipeline: intent, detection, verification, credit, reversal.

The pipeline is rail-agnostic. Each rail only has to answer three questions:

* what does the client need to do (instructions),
* has the money actually arrived (detection), and
* is it genuinely ours to keep (verification).

Everything after that - the ledger postings, the balances, the reserve
window, the reversal - is shared.

The one rule that overrides every other consideration: **credit only from an
independently verified external fact**. Never from the client's own request,
and never from a webhook body that has not been checked against the
provider.
"""

from __future__ import annotations

import secrets
import string
from decimal import Decimal, ROUND_HALF_UP

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from veltrix_app.models import (
    Asset,
    AssetBalance,
    AuditLog,
    BankDeposit,
    CardDeposit,
    CryptoDeposit,
    Deposit,
    DepositAddress,
    DepositProvider,
    LedgerEntry,
    Network,
    User,
)
from veltrix_app.services import ledger
from veltrix_app.services.ledger import Posting

ZERO = Decimal("0")
REFERENCE_ALPHABET = string.ascii_uppercase + string.digits


class DepositError(Exception):
    """Base class for deposit pipeline failures."""


class InvalidTransition(DepositError):
    """Raised when a deposit is moved into a state it cannot reach."""


class KycRequired(DepositError):
    """Raised when a client without approved KYC tries to deposit."""


class AccountFrozen(DepositError):
    """Raised when a frozen client tries to deposit."""


#: Allowed state machine moves. Anything not listed here is rejected, which
#: keeps a late or duplicated webhook from resurrecting a closed deposit.
ALLOWED_TRANSITIONS = {
    Deposit.Status.PENDING: {
        Deposit.Status.DETECTED,
        Deposit.Status.MANUAL_REVIEW,
        Deposit.Status.REJECTED,
        Deposit.Status.EXPIRED,
    },
    Deposit.Status.DETECTED: {
        Deposit.Status.VERIFYING,
        Deposit.Status.VERIFIED,
        Deposit.Status.MANUAL_REVIEW,
        Deposit.Status.REJECTED,
    },
    Deposit.Status.VERIFYING: {
        Deposit.Status.VERIFIED,
        Deposit.Status.MANUAL_REVIEW,
        Deposit.Status.REJECTED,
    },
    Deposit.Status.VERIFIED: {
        Deposit.Status.CREDITED,
        Deposit.Status.MANUAL_REVIEW,
        Deposit.Status.REJECTED,
    },
    Deposit.Status.CREDITED: {
        Deposit.Status.RESERVED,
        Deposit.Status.AVAILABLE,
        Deposit.Status.REVERSED,
    },
    Deposit.Status.RESERVED: {
        Deposit.Status.AVAILABLE,
        Deposit.Status.REVERSED,
    },
    Deposit.Status.AVAILABLE: {
        Deposit.Status.REVERSED,
    },
    Deposit.Status.MANUAL_REVIEW: {
        Deposit.Status.VERIFIED,
        Deposit.Status.CREDITED,
        Deposit.Status.AVAILABLE,
        Deposit.Status.RESERVED,
        Deposit.Status.REJECTED,
        Deposit.Status.REVERSED,
    },
    Deposit.Status.REJECTED: {Deposit.Status.MANUAL_REVIEW},
    Deposit.Status.REVERSED: set(),
    # A late chain payment can still arrive after provider expiry; keep it in
    # manual review rather than silently losing the receipt or auto-crediting.
    Deposit.Status.EXPIRED: {Deposit.Status.MANUAL_REVIEW},
}


def transition(deposit: Deposit, new_status: str, *, reason: str = "") -> Deposit:
    """Move a deposit to a new state, rejecting illegal moves."""
    allowed = ALLOWED_TRANSITIONS.get(deposit.status, set())
    if new_status not in allowed:
        raise InvalidTransition(
            f"Deposit {deposit.id} cannot move from {deposit.status} to {new_status}."
        )
    deposit.status = new_status
    if reason:
        deposit.review_reason = reason
    deposit.updated_at = timezone.now()
    deposit.save(update_fields=["status", "review_reason", "updated_at"])
    return deposit


def generate_reference() -> str:
    """A short, unambiguous reference the client can put on a transfer."""
    body = "".join(secrets.choice(REFERENCE_ALPHABET) for _ in range(10))
    return f"VELT-{body}"


def _kyc_profile(user: User):
    from veltrix_app.services.kyc import ensure_profile

    return ensure_profile(user)


def _require_deposits_enabled() -> None:
    if not getattr(settings, "FUNDING_DEPOSITS_ENABLED", False):
        raise DepositError("Real deposits are not enabled on this deployment.")


def _require_kyc(user: User) -> None:
    if not getattr(settings, "FUNDING_REQUIRE_KYC", True):
        return
    profile = _kyc_profile(user)
    if not profile.is_approved:
        raise KycRequired("Identity verification is required before depositing.")


def _require_not_frozen(user: User) -> None:
    if user.funds_frozen:
        raise AccountFrozen("This account is frozen. Contact support.")


# ---------------------------------------------------------------------------
# Intent
# ---------------------------------------------------------------------------

@transaction.atomic
def create_deposit_intent(
    user: User,
    provider: DepositProvider,
    asset: Asset,
    amount: Decimal,
    *,
    idempotency_key: str,
    network: Network | None = None,
) -> Deposit:
    """Record the client's intent to deposit and produce the instructions.

    Nothing is credited here. The intent only tells the client where to send
    the money and gives the rail a stable reference to attribute it to.
    """
    # Serialize this user's intent creation. Besides refreshing a possibly
    # stale frozen flag, the row lock makes concurrent retries with the same
    # idempotency key observe the winner before calling an external provider.
    user = User.objects.select_for_update().get(pk=user.pk)
    _require_deposits_enabled()
    _require_kyc(user)
    _require_not_frozen(user)

    if amount <= ZERO:
        raise DepositError("Deposit amount must be positive.")

    existing = Deposit.objects.filter(user=user, idempotency_key=idempotency_key).first()
    if existing is not None:
        existing_amount = existing.price_amount if provider.code == "nowpayments" else existing.expected_amount
        if (existing.provider_id != provider.id or existing.asset_id != asset.id
                or existing_amount != amount
                or (network is not None and (not hasattr(existing, "crypto") or existing.crypto.network_id != network.id))):
            raise DepositError("This idempotency key was already used for a different deposit request.")
        if existing.provider_creation_uncertain:
            raise DepositError("A prior NOWPayments payment creation is unresolved. Reconcile it before requesting another deposit.")
        if provider.code == "nowpayments" and validate_nowpayments_quote(existing):
            raise DepositError("This NOWPayments quote expired or changed. Request a new quote.")
        return existing

    provider_assets = list(provider.supported_assets.all())
    if provider_assets and asset not in provider_assets:
        raise DepositError(f"Provider {provider.code} does not accept {asset.code}.")

    if provider.code == "nowpayments":
        if Deposit.objects.filter(user=user, provider=provider, provider_creation_uncertain=True).exists():
            raise DepositError("A prior NOWPayments payment creation is unresolved. Reconcile it before requesting another deposit.")
        # Provider/network minimums are denominated in the quoted crypto asset,
        # while this request is a USD notional. Do not compare unlike units.
        # NOWPayments will reject a below-minimum payment during creation.
        if provider.fee_percent or provider.fee_fixed:
            raise DepositError(
                "NOWPayments Veltrix fees are unavailable until a separate fee collection flow is approved."
            )
    else:
        if provider.min_deposit and amount < provider.min_deposit:
            raise DepositError(f"Minimum deposit for {provider.code} is {provider.min_deposit} {asset.code}.")
        if provider.max_deposit and amount > provider.max_deposit:
            raise DepositError(f"Maximum deposit for {provider.code} is {provider.max_deposit} {asset.code}.")
        if network is not None and network.min_deposit and amount < network.min_deposit:
            raise DepositError(f"Minimum deposit for {network.chain} is {network.min_deposit} {asset.code}.")

    deposit = Deposit.objects.create(
        user=user,
        rail=provider.rail,
        asset=asset,
        provider=provider,
        expected_amount=amount,
        price_amount=amount if provider.code == "nowpayments" else None,
        price_currency="usd" if provider.code == "nowpayments" else "",
        idempotency_key=idempotency_key,
        reference=generate_reference(),
        provider_ref=generate_reference(),
        fee_amount=(ZERO if provider.code == "nowpayments" else
            Decimal(str(provider.fee_percent)) / Decimal("100") * amount
            + Decimal(str(provider.fee_fixed))),
    )

    if provider.rail == Deposit.Rail.BANK:
        _create_bank_intent(deposit)
    elif provider.rail == Deposit.Rail.CRYPTO:
        if network is None:
            raise DepositError("A crypto deposit needs a network.")
        _create_crypto_intent(deposit, network)
    elif provider.rail == Deposit.Rail.CARD:
        _create_card_intent(deposit)

    AuditLog.objects.create(
        user=user,
        actor=user,
        action="deposit.intent_created",
        details=f"deposit={deposit.id} rail={deposit.rail} asset={deposit.asset_id} amount={amount}",
    )
    return deposit


def _create_bank_intent(deposit: Deposit) -> None:
    """A bank deposit is attributed by a dedicated reference, not by narration.

    The reference is the identity of the deposit: whoever includes it in the
    transfer is the one credited, and the amount is checked against it.
    """
    collection = getattr(settings, "FUNDING_COLLECTION_ACCOUNT", {}) or {}
    deposit.reference = deposit.provider_ref
    bank = BankDeposit.objects.create(
        deposit=deposit,
        provider_reference=deposit.reference,
        provider_account=collection.get("account_number", ""),
    )
    deposit.instructions = {
        "bank_name": collection.get("bank_name", ""),
        "account_number": collection.get("account_number", ""),
        "account_name": collection.get("account_name", ""),
        "reference": deposit.reference,
        "amount": str(deposit.expected_amount),
        "currency": deposit.asset.code,
        "note": collection.get("note", ""),
    }
    deposit.save(update_fields=["reference", "instructions"])
    bank.save(update_fields=["provider_account"])


def _create_crypto_intent(deposit: Deposit, network: Network) -> None:
    """Create provider-issued instructions; never invent a wallet address."""
    if (deposit.asset.code != "USDT" or network.chain.upper() not in ("TRON", "TRON MAINNET")
            or network.token_standard.upper() != "TRC20" or network.is_testnet or not network.is_active):
        raise DepositError("Only an active mainnet USDT TRC20 network is supported.")
    if deposit.provider.code != "nowpayments":
        raise DepositError("No live crypto deposit provider is configured.")
    from veltrix_app.services.adapters import get_adapter
    # Make the selected chain available to the adapter while it validates the
    # provider quote; the outer transaction rolls this row back on any failure.
    crypto = CryptoDeposit.objects.create(deposit=deposit, network=network, address="",
        required_confirmations=network.required_confirmations)
    from veltrix_app.services.adapters import PaymentCreationUncertain
    try:
        instructions = get_adapter(deposit.provider).create_intent(deposit)
    except PaymentCreationUncertain as exc:
        deposit.provider_ref = exc.provider_ref
        deposit.provider_creation_uncertain = True
        deposit.instructions = {
            "provider": "NOWPayments",
            "order_id": deposit.reference,
            "message": "Payment creation could not be confirmed. Do not submit another deposit until support reconciles this reference.",
        }
        send_to_manual_review(
            deposit,
            reason=f"NOWPayments payment creation is uncertain; reconcile order reference {deposit.reference} before retrying.",
        )
        deposit.save(update_fields=["provider_ref", "provider_creation_uncertain", "instructions", "updated_at"])
        return
    crypto.address = instructions["address"]
    crypto.save(update_fields=["address"])
    deposit.instructions = instructions
    deposit.save(update_fields=["instructions"])


def _create_card_intent(deposit: Deposit) -> None:
    """A card deposit is started at the provider's hosted checkout.

    Veltrix never sees the card number: the client is handed a session to
    complete at the payment provider.
    """
    card = CardDeposit.objects.create(deposit=deposit)
    deposit.instructions = {
        "provider": deposit.provider.code,
        "psp_session_id": card.psp_session_id,
        "amount": str(deposit.expected_amount),
        "currency": deposit.asset.code,
        "note": "Complete the payment on the provider's secure checkout page.",
    }
    deposit.save(update_fields=["instructions"])


# ---------------------------------------------------------------------------
# Detection and verification
# ---------------------------------------------------------------------------

@transaction.atomic
def mark_detected(
    deposit: Deposit,
    *,
    amount: Decimal | None = None,
    provider_ref: str | None = None,
    actor: User | None = None,
) -> Deposit:
    """Record that the money has been seen on the rail."""
    if deposit.is_credited:
        return deposit
    if amount is not None:
        deposit.received_amount = amount
    if provider_ref:
        deposit.provider_ref = provider_ref
    deposit.updated_at = timezone.now()
    deposit.save(update_fields=["received_amount", "provider_ref", "updated_at"])
    if deposit.status == Deposit.Status.PENDING:
        transition(deposit, Deposit.Status.DETECTED)
    AuditLog.objects.create(
        user=deposit.user,
        actor=actor,
        action="deposit.detected",
        details=f"deposit={deposit.id} amount={deposit.received_amount}",
    )
    return deposit


@transaction.atomic
def verify_deposit(deposit: Deposit, *, actor: User | None = None, reason: str = "") -> Deposit:
    """Move a detected deposit through verification to verified."""
    if deposit.status == Deposit.Status.VERIFIED:
        return deposit
    if deposit.status == Deposit.Status.DETECTED:
        transition(deposit, Deposit.Status.VERIFYING)
    if deposit.status == Deposit.Status.VERIFYING:
        transition(deposit, Deposit.Status.VERIFIED)
    if reason:
        deposit.review_reason = reason
        deposit.save(update_fields=["review_reason"])
    AuditLog.objects.create(
        user=deposit.user,
        actor=actor,
        action="deposit.verified",
        details=f"deposit={deposit.id}",
    )
    return deposit


@transaction.atomic
def send_to_manual_review(deposit: Deposit, *, reason: str, actor: User | None = None) -> Deposit:
    """Park a deposit for a human to look at.

    Nothing is credited and nothing is rejected: a review is the safe default
    whenever the automated checks do not line up.
    """
    if deposit.status == Deposit.Status.REVERSED:
        raise InvalidTransition(f"Deposit {deposit.id} is closed ({deposit.status}).")
    if deposit.status != Deposit.Status.MANUAL_REVIEW:
        transition(deposit, Deposit.Status.MANUAL_REVIEW, reason=reason)
    AuditLog.objects.create(
        user=deposit.user,
        actor=actor,
        action="deposit.manual_review",
        details=f"deposit={deposit.id} reason={reason}",
    )
    return deposit


def validate_nowpayments_quote(deposit: Deposit, *, now=None, require_confirmed=False) -> str | None:
    """Return a safe reason if the persisted USD-to-USDT payment quote is unusable."""
    now = now or timezone.now()
    crypto = getattr(deposit, "crypto", None)
    instructions = deposit.instructions or {}
    try:
        instruction_price = Decimal(str(instructions.get("price_amount")))
        instruction_amount = Decimal(str(instructions.get("amount")))
    except (InvalidOperation, TypeError, ValueError):
        return "The payment quote is expired or its stored amount, currency, network, or provider reference changed."
    if (deposit.provider.code != "nowpayments" or (require_confirmed and not deposit.quote_confirmed_at)
            or deposit.price_amount is None
            or deposit.price_amount <= ZERO or deposit.price_currency.lower() != "usd"
            or deposit.quoted_amount is None or deposit.quoted_amount <= ZERO
            or deposit.quoted_currency.lower() != "usdttrc20"
            or deposit.expected_amount != deposit.quoted_amount
            or (deposit.network_precision is not None and
                deposit.quoted_amount != deposit.quoted_amount.quantize(Decimal(1).scaleb(-deposit.network_precision)))
            or deposit.quoted_network.lower() != "tron (trc20)"
            or not deposit.provider_ref or deposit.quote_expires_at is None
            or not timezone.is_aware(deposit.quote_expires_at)
            or (not deposit.quote_confirmed_at and deposit.quote_expires_at <= now)
            or instruction_price != deposit.price_amount
            or str(instructions.get("price_currency", "")).lower() != "usd"
            or instruction_amount != deposit.quoted_amount
            or str(instructions.get("currency", "")).upper() != "USDT"
            or not crypto or crypto.network.chain.upper() != "TRON"
            or crypto.network.token_standard.upper() != "TRC20"
            or crypto.network.is_testnet):
        return "The payment quote is expired or its stored amount, currency, network, or provider reference changed."
    return None


def check_amount(deposit: Deposit) -> str | None:
    """Compare what arrived against what was expected.

    Returns ``None`` when the amounts are acceptable, otherwise a short
    reason describing the problem.
    """
    received = deposit.received_amount
    if received is None:
        return "No amount received yet."
    tolerance = Decimal(str(getattr(settings, "FUNDING_UNDERPAY_TOLERANCE_PERCENT", "1")))
    floor = (deposit.expected_amount * (Decimal("100") - tolerance) / Decimal("100")).quantize(
        Decimal("0.000000000000000001"), rounding=ROUND_HALF_UP
    )
    if received < floor:
        return f"Underpaid: expected {deposit.expected_amount}, received {received}."
    return None


# ---------------------------------------------------------------------------
# Credit
# ---------------------------------------------------------------------------

@transaction.atomic
def credit_deposit(deposit: Deposit, *, actor: User | None = None) -> Deposit:
    """Credit a verified deposit to the client's ledger balance.

    This is the only place client money is created on the ledger, and it is
    idempotent: calling it twice for the same deposit posts once.
    """
    if deposit.is_credited:
        return deposit
    if deposit.provider.code == "nowpayments" and (deposit.fee_amount or ZERO) != ZERO:
        raise DepositError("NOWPayments Veltrix fees cannot be deducted from settlement.")
    if deposit.status not in (Deposit.Status.VERIFIED, Deposit.Status.MANUAL_REVIEW):
        raise InvalidTransition(
            f"Deposit {deposit.id} must be verified before it can be credited (is {deposit.status})."
        )

    problem = check_amount(deposit)
    if problem:
        raise DepositError(problem)

    gross = deposit.received_amount or deposit.expected_amount
    fee = deposit.fee_amount or ZERO
    net = gross - fee
    if net <= ZERO:
        raise DepositError("The deposit amount does not cover the fee.")

    asset = deposit.asset
    customer = ledger.customer_account(asset, deposit.user)
    fee_account = ledger.fee_revenue_account(asset)

    postings = []
    if deposit.rail == Deposit.Rail.CARD:
        # The money is sitting at the payment provider until it settles, so it
        # lands in the in-transit account and moves to treasury on settlement.
        postings.append(
            Posting(account=ledger.deposits_in_transit_account(asset), side="D", amount=gross)
        )
    else:
        postings.append(Posting(account=ledger.treasury_account(asset), side="D", amount=gross))

    bucket = "reserved" if deposit.provider.reserve_days else "available"
    postings.append(Posting(account=customer, side="C", amount=net, bucket=bucket))
    if fee > ZERO:
        postings.append(Posting(account=fee_account, side="C", amount=fee))

    entry = ledger.record_entry(
        reference_type=LedgerEntry.ReferenceType.DEPOSIT,
        reference_id=str(deposit.id),
        postings=postings,
        asset=asset,
        note=f"Deposit {deposit.id} credited",
        actor=actor,
    )

    deposit.ledger_entry = entry
    deposit.credited_at = timezone.now()
    deposit.received_amount = gross
    deposit.reserved_amount = net if bucket == "reserved" else ZERO
    deposit.updated_at = timezone.now()

    if deposit.provider.reserve_days:
        deposit.available_at = deposit.credited_at + timezone.timedelta(days=deposit.provider.reserve_days)
        deposit.status = Deposit.Status.CREDITED
        deposit.save(
            update_fields=[
                "ledger_entry",
                "credited_at",
                "available_at",
                "received_amount",
                "reserved_amount",
                "status",
                "updated_at",
            ]
        )
        transition(deposit, Deposit.Status.RESERVED)
    else:
        deposit.available_at = deposit.credited_at
        deposit.status = Deposit.Status.CREDITED
        deposit.save(
            update_fields=[
                "ledger_entry",
                "credited_at",
                "available_at",
                "received_amount",
                "reserved_amount",
                "status",
                "updated_at",
            ]
        )
        transition(deposit, Deposit.Status.AVAILABLE)

    ledger.assert_balance_consistent(deposit.user, deposit.asset)

    AuditLog.objects.create(
        user=deposit.user,
        actor=actor,
        action="deposit.credited",
        details=(
            f"deposit={deposit.id} gross={gross} fee={fee} net={net} "
            f"asset={asset.code} bucket={bucket} entry={entry.id}"
        ),
    )
    return deposit


@transaction.atomic
def release_reserve(deposit: Deposit, *, actor: User | None = None) -> Deposit:
    """Move a card deposit out of the reserve window and into the balance.

    The money was already on the ledger when the deposit was credited, so this
    only moves it between the reserved and available slices of the cached
    balance. No ledger entry is written.
    """
    if deposit.status != Deposit.Status.RESERVED:
        return deposit
    if deposit.available_at and deposit.available_at > timezone.now():
        return deposit

    card = getattr(deposit, "card", None)
    if card is not None and card.dispute_status == CardDeposit.DisputeStatus.OPEN:
        return deposit

    ledger.move_reserved_to_available(deposit.user, deposit.asset, deposit.net_amount)
    transition(deposit, Deposit.Status.AVAILABLE)
    deposit.updated_at = timezone.now()
    deposit.save(update_fields=["updated_at"])
    AuditLog.objects.create(
        user=deposit.user,
        actor=actor,
        action="deposit.reserve_released",
        details=f"deposit={deposit.id} amount={deposit.net_amount}",
    )
    return deposit


# ---------------------------------------------------------------------------
# Reversal
# ---------------------------------------------------------------------------

@transaction.atomic
def reverse_deposit(
    deposit: Deposit,
    *,
    reason: str,
    actor: User | None = None,
    freeze: bool = False,
) -> Deposit:
    """Undo a credited deposit as one locked, auditable operation.

    A spent deposit may leave a negative ledger balance because this model has
    no debt or recovery policy. In that case the account is frozen and the
    deficit is explicitly audited in the same transaction as the reversal.
    Any database failure rolls back the posting, state transition, freeze, and
    audit records together.
    """
    # Lock the state row so concurrent retries cannot independently process a
    # stale caller instance. The ledger reversal reference is an additional
    # idempotency guard.
    deposit = Deposit.objects.select_for_update().get(pk=deposit.pk)
    if deposit.status == Deposit.Status.REVERSED:
        return deposit
    if not deposit.is_credited:
        raise InvalidTransition(f"Deposit {deposit.id} has not been credited.")
    if deposit.ledger_entry_id is None:
        raise DepositError(f"Deposit {deposit.id} has no ledger entry to reverse.")

    original = deposit.ledger_entry
    customer_credits = sum(
        (posting.amount for posting in original.postings.select_related("account").all()
         if posting.account.is_customer_account and posting.side == "C"),
        ZERO,
    )
    AssetBalance.objects.select_for_update().get(
        user_id=deposit.user_id, asset_id=deposit.asset_id
    )
    current_total = ledger.ledger_balance(deposit.user, deposit.asset)
    deficit = max(ZERO, customer_credits - current_total)
    should_freeze = freeze or deficit > ZERO

    if should_freeze:
        locked_user = User.objects.select_for_update().get(pk=deposit.user_id)
        if not locked_user.funds_frozen:
            locked_user.funds_frozen = True
            locked_user.save(update_fields=["funds_frozen"])
        deposit.user = locked_user
    if deficit > ZERO:
        AuditLog.objects.create(
            user=deposit.user,
            actor=actor,
            action="deposit.reversal_deficit",
            details=(
                f"deposit={deposit.id} asset={deposit.asset.code} "
                f"balance_before={current_total} reversal_amount={customer_credits} "
                f"projected_deficit={deficit}; account frozen; reason={reason}"
            ),
        )

    ledger.reverse_entry(original, reference_id=f"rev-{deposit.id}", reason=reason, actor=actor)

    if deposit.rail == Deposit.Rail.CARD:
        card = getattr(deposit, "card", None)
        if card is not None:
            card.dispute_status = CardDeposit.DisputeStatus.LOST
            card.dispute_amount = deposit.net_amount
            card.save(update_fields=["dispute_status", "dispute_amount"])
        bank = getattr(deposit, "bank", None)
        if bank is not None:
            bank.reversed_at = timezone.now()
            bank.save(update_fields=["reversed_at"])

    deposit.review_reason = reason
    deposit.updated_at = timezone.now()
    deposit.save(update_fields=["review_reason", "updated_at"])
    transition(deposit, Deposit.Status.REVERSED)

    AuditLog.objects.create(
        user=deposit.user,
        actor=actor,
        action="deposit.reversed",
        details=f"deposit={deposit.id} reason={reason} frozen={should_freeze} deficit={deficit}",
    )
    return deposit


@transaction.atomic
def settle_card_deposit(deposit: Deposit, *, actor: User | None = None) -> Deposit:
    """Move a captured card payment from in-transit into the treasury."""
    if deposit.rail != Deposit.Rail.CARD or not deposit.is_credited:
        return deposit
    card = getattr(deposit, "card", None)
    if card is None or card.settled_at is not None:
        return deposit

    asset = deposit.asset
    gross = deposit.received_amount or deposit.expected_amount
    postings = [
        Posting(account=ledger.treasury_account(asset), side="D", amount=gross),
        Posting(account=ledger.deposits_in_transit_account(asset), side="C", amount=gross),
    ]
    ledger.record_entry(
        reference_type=LedgerEntry.ReferenceType.DEPOSIT,
        reference_id=f"settle-{deposit.id}",
        postings=postings,
        asset=asset,
        note=f"Card deposit {deposit.id} settled",
        actor=actor,
    )
    card.settled_at = timezone.now()
    card.save(update_fields=["settled_at"])
    return deposit
