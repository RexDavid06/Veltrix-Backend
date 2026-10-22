from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        CLIENT = "client", "Client"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CLIENT)
    is_verified = models.BooleanField(default=False)
    funds_frozen = models.BooleanField(
        default=False,
        help_text="Blocks withdrawals and new deposits until an admin clears it.",
    )

    def is_admin(self):
        return self.role == self.Role.ADMIN or self.is_staff

    def __str__(self):
        return self.get_full_name() or self.email or self.username


class DemoAccount(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="demo_account")
    currency = models.CharField(max_length=8, default="USD")
    cash_balance = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"DemoAccount({self.user_id}, {self.currency})"


class Instrument(models.Model):
    symbol = models.CharField(max_length=24, unique=True)
    name = models.CharField(max_length=120)
    asset_class = models.CharField(max_length=32)
    currency = models.CharField(max_length=8, default="USD")
    price = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("0"))
    change_percent = models.DecimalField(max_digits=8, decimal_places=4, default=Decimal("0"))
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["asset_class", "symbol"]

    def __str__(self):
        return f"{self.symbol} ({self.name})"


class Position(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="positions")
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="positions")
    quantity = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal("0"))
    avg_price = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("0"))
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("user", "instrument")

    @property
    def market_value(self):
        return self.quantity * self.instrument.price

    @property
    def cost_basis(self):
        return self.quantity * self.avg_price

    @property
    def unrealized_pnl(self):
        return self.market_value - self.cost_basis


class WatchlistItem(models.Model):
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="watchlist")
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="watchlist_items")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        unique_together = ("user", "instrument")


class Transaction(models.Model):
    SIDE_CHOICES = [
        ("buy", "Buy"),
        ("sell", "Sell"),
        ("topup", "Top-Up"),
    ]

    STATUS_CHOICES = [
        ("simulated", "Simulated"),
        ("pending", "Pending"),
        ("completed", "Completed"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="transactions")
    instrument = models.ForeignKey(Instrument, on_delete=models.SET_NULL, null=True, blank=True, related_name="transactions")
    symbol = models.CharField(max_length=24)
    side = models.CharField(max_length=12, choices=SIDE_CHOICES)
    quantity = models.DecimalField(max_digits=24, decimal_places=8, default=Decimal("0"))
    execution_price = models.DecimalField(max_digits=18, decimal_places=8, default=Decimal("0"))
    amount = models.DecimalField(max_digits=18, decimal_places=2, default=Decimal("0.00"))
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="simulated")
    note = models.CharField(max_length=255, blank=True, null=True)
    created_at = models.DateTimeField(default=timezone.now)


class SupportTicket(models.Model):
    STATUS_CHOICES = [
        ("open", "Open"),
        ("in_progress", "In progress"),
        ("resolved", "Resolved"),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="support_tickets")
    subject = models.CharField(max_length=160)
    message = models.TextField()
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="open")
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)


class AuditLog(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_events")
    actor = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="audit_actions")
    action = models.CharField(max_length=160)
    details = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"{self.action} @ {self.created_at.isoformat()}"


# --- Support chat models ---
class Conversation(models.Model):
    """
    Conversation between a client (user) and support/admins.
    """

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="conversations")
    assigned_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="assigned_conversations",
        null=True,
        blank=True,
    )
    subject = models.CharField(max_length=160, blank=True, default="General Support")
    is_active = models.BooleanField(default=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [
            models.Index(fields=["user", "is_active"]),
            models.Index(fields=["assigned_to", "is_active"]),
            models.Index(fields=["updated_at"]),
        ]

    def __str__(self):
        return f"Conversation({self.id}) user={self.user_id} subject={self.subject}"

    @property
    def is_closed(self):
        return not self.is_active

    def unread_count_for(self, user):
        # Number of messages in this conversation the given user has not yet
        # read. Staff/admin see every message authored by someone else; the
        # owning client only counts staff replies. Requests from anyone who is
        # neither staff nor the conversation owner always return 0 so this
        # helper can never leak a count across accounts.
        if not user or not user.is_authenticated:
            return 0
        is_staff = bool(getattr(user, "is_staff", False) or (hasattr(user, "is_admin") and user.is_admin()))
        if is_staff:
            return self.messages.exclude(sender=user).filter(is_read=False).count()
        if user.id != self.user_id:
            return 0
        return self.messages.filter(is_read=False).exclude(sender=user).filter(sender__is_staff=True).count()


class Message(models.Model):
    conversation = models.ForeignKey(Conversation, on_delete=models.CASCADE, related_name="messages")
    sender = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="sent_messages")
    content = models.TextField()
    is_read = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["created_at"]
        indexes = [
            models.Index(fields=["conversation", "is_read"]),
            models.Index(fields=["conversation", "created_at"]),
        ]

    def __str__(self):
        return f"Message({self.id}) conv={self.conversation_id} sender={self.sender_id}"


# ---------------------------------------------------------------------------
# Real funds: assets, KYC, double-entry ledger and deposits.
#
# Everything below this line models *real* client money. It is deliberately
# kept separate from DemoAccount/Transaction above, which remain the simulated
# paper-trading economy and are never written by the funding pipeline.
# ---------------------------------------------------------------------------

#: Monetary columns use 38 digits with 18 decimal places so an 18-decimal
#: token and a 2-decimal fiat amount can share one schema without churn.
MONEY_DIGITS = (38, 18)
ZERO = Decimal("0")


def money():
    return models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)


class KycProfile(models.Model):
    """Client identity-verification status.

    Deliberately separate from ``User.is_verified``, which the email
    verification flow sets and which means "email confirmed", not
    "identity verified".
    """

    class Status(models.TextChoices):
        UNVERIFIED = "unverified", "Unverified"
        PENDING = "pending", "Pending"
        IN_REVIEW = "in_review", "In review"
        APPROVED = "approved", "Approved"
        REJECTED = "rejected", "Rejected"
        EXPIRED = "expired", "Expired"

    class Tier(models.TextChoices):
        TIER_1 = "tier_1", "Tier 1"
        TIER_2 = "tier_2", "Tier 2"
        TIER_3 = "tier_3", "Tier 3"

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="kyc_profile")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.UNVERIFIED)
    tier = models.CharField(max_length=16, choices=Tier.choices, default=Tier.TIER_1)
    full_name = models.CharField(max_length=160, blank=True, default="")
    phone = models.CharField(max_length=32, blank=True, default="")
    country = models.CharField(max_length=64, blank=True, default="")
    provider_ref = models.CharField(max_length=128, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="kyc_reviews"
    )
    verified_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    @property
    def is_approved(self):
        return self.status == self.Status.APPROVED

    def __str__(self):
        return f"KycProfile({self.user_id}, {self.status})"


class Asset(models.Model):
    """A balanceable unit of value: a fiat currency or a crypto asset."""

    class AssetClass(models.TextChoices):
        FIAT = "fiat", "Fiat"
        CRYPTO = "crypto", "Crypto"

    code = models.CharField(max_length=16, unique=True)
    display_name = models.CharField(max_length=64)
    decimals = models.PositiveSmallIntegerField(default=2)
    asset_class = models.CharField(max_length=8, choices=AssetClass.choices, default=AssetClass.FIAT)
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=100)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["sort_order", "code"]

    def __str__(self):
        return self.code


class Network(models.Model):
    """A deposit network for a crypto asset (chain + token standard)."""

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="networks")
    chain = models.CharField(max_length=32)
    token_standard = models.CharField(max_length=16, default="native")
    contract_address = models.CharField(max_length=128, blank=True, default="")
    decimals = models.PositiveSmallIntegerField(default=8)
    required_confirmations = models.PositiveIntegerField(default=3)
    min_deposit = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    dust_threshold = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    is_testnet = models.BooleanField(default=True)
    is_active = models.BooleanField(default=False)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["asset__code", "chain"]
        constraints = [
            models.UniqueConstraint(
                fields=["asset", "chain", "token_standard", "contract_address"],
                name="uniq_network",
            )
        ]

    def __str__(self):
        return f"{self.asset_id}:{self.chain}:{self.token_standard}"


class DepositProvider(models.Model):
    """A configured funding rail (bank, card or crypto provider)."""

    class Rail(models.TextChoices):
        CRYPTO = "crypto", "Crypto"
        BANK = "bank", "Bank transfer"
        CARD = "card", "Card"

    code = models.CharField(max_length=48, unique=True)
    display_name = models.CharField(max_length=120)
    rail = models.CharField(max_length=8, choices=Rail.choices)
    is_active = models.BooleanField(default=False)
    is_test = models.BooleanField(default=True)
    config = models.JSONField(default=dict, blank=True)
    supported_assets = models.ManyToManyField(Asset, blank=True, related_name="providers")
    min_deposit = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    max_deposit = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    fee_percent = models.DecimalField(max_digits=8, decimal_places=4, default=ZERO)
    fee_fixed = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    reserve_days = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["rail", "code"]

    def __str__(self):
        return self.code


class LedgerAccount(models.Model):
    """A chart-of-accounts row, scoped to one asset (and one customer)."""

    class NormalSide(models.TextChoices):
        DEBIT = "D", "Debit"
        CREDIT = "C", "Credit"

    TREASURY = "1000"
    DEPOSITS_IN_TRANSIT = "1010"
    WITHDRAWALS_IN_TRANSIT = "1020"
    CUSTOMER_RECEIVABLE = "1090"
    FX_POSITION = "1100"
    CUSTOMER = "2000"
    WITHDRAWALS_PAYABLE = "2010"
    ADJUSTMENT_CLEARING = "2090"
    RECONCILIATION_CLEARING = "3000"
    DEPOSIT_FEE_REVENUE = "4000"
    WITHDRAWAL_FEE_REVENUE = "4010"
    NETWORK_FEE_EXPENSE = "5000"
    ADJUSTMENT_LOSS = "5090"

    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="ledger_accounts")
    code = models.CharField(max_length=8)
    user = models.ForeignKey(User, on_delete=models.PROTECT, null=True, blank=True, related_name="ledger_accounts")
    normal_side = models.CharField(max_length=1, choices=NormalSide.choices)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["code"]
        constraints = [
            # Split in two because SQL treats NULLs as distinct: a single
            # constraint on (asset, code, user) would happily allow several
            # platform accounts (user IS NULL) for the same asset and code.
            models.UniqueConstraint(
                fields=["asset", "code", "user"],
                name="uniq_ledger_account_platform",
                condition=models.Q(user__isnull=True),
            ),
            models.UniqueConstraint(
                fields=["asset", "code", "user"],
                name="uniq_ledger_account",
                condition=models.Q(user__isnull=False),
            ),
        ]
        indexes = [models.Index(fields=["asset", "code"])]

    def __str__(self):
        return f"{self.code}:{self.asset_id}:{self.user_id}"

    @property
    def is_customer_account(self):
        return self.code == self.CUSTOMER


class LedgerEntry(models.Model):
    """An immutable, always-balanced group of postings."""

    class ReferenceType(models.TextChoices):
        DEPOSIT = "deposit", "Deposit"
        WITHDRAWAL = "withdrawal", "Withdrawal"
        REVERSAL = "reversal", "Reversal"
        ADJUSTMENT = "adjustment", "Adjustment"
        CONVERSION = "conversion", "Conversion"
        RESERVE = "reserve", "Reserve movement"

    reference_type = models.CharField(max_length=24, choices=ReferenceType.choices)
    reference_id = models.CharField(max_length=64)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="ledger_entries")
    reverses_entry = models.ForeignKey(
        "self", on_delete=models.PROTECT, null=True, blank=True, related_name="reversals"
    )
    note = models.CharField(max_length=255, blank=True, default="")
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="ledger_entries")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["reference_type", "reference_id", "asset"],
                name="uniq_ledger_entry_ref",
            )
        ]

    def __str__(self):
        return f"LedgerEntry({self.reference_type}:{self.reference_id}:{self.asset_id})"

    def save(self, *args, **kwargs):
        if self.pk and LedgerEntry.objects.filter(pk=self.pk).exists():
            raise ValueError("Ledger entries are immutable; post a reversing entry instead.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Ledger entries are immutable; post a reversing entry instead.")


class LedgerPosting(models.Model):
    """One line of a ledger entry.

    ``amount`` is always positive and the side is explicit, so a liability can
    be reduced (and go negative on a reversal) without ever storing a signed
    magnitude.
    """

    class Side(models.TextChoices):
        DEBIT = "D", "Debit"
        CREDIT = "C", "Credit"

    entry = models.ForeignKey(LedgerEntry, on_delete=models.PROTECT, related_name="postings")
    account = models.ForeignKey(LedgerAccount, on_delete=models.PROTECT, related_name="postings")
    side = models.CharField(max_length=1, choices=Side.choices)
    amount = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1])
    # Which balance slice this posting touched. Only meaningful for customer
    # accounts: "available" or "reserved" ("" for platform accounts). It is
    # persisted so a reversal can unwind exactly the slice the original credit
    # landed in, instead of guessing from the aggregate balance.
    bucket = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        ordering = ["id"]
        constraints = [
            models.UniqueConstraint(fields=["entry", "account"], name="uniq_posting_account")
        ]

    def __str__(self):
        return f"LedgerPosting({self.account_id} {self.side} {self.amount})"

    @property
    def signed_amount(self):
        value = self.amount if self.side == self.Side.DEBIT else -self.amount
        return value

    def save(self, *args, **kwargs):
        if self.pk and LedgerPosting.objects.filter(pk=self.pk).exists():
            raise ValueError("Ledger postings are immutable.")
        return super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValueError("Ledger postings are immutable.")


class AssetBalance(models.Model):
    """Cached balance per (user, asset).

    The ledger is authoritative. ``available`` and ``reserved`` together must
    always equal the ledger-derived ``CUSTOMER:{user}:{asset}`` balance; the
    reconciliation job asserts it and freezes the account on a mismatch.
    ``pending`` is a memo of in-flight deposits and is deliberately *not* on
    the ledger.
    """

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="asset_balances")
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="asset_balances")
    available = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    reserved = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    pending = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["asset__code"]
        constraints = [
            models.UniqueConstraint(fields=["user", "asset"], name="uniq_asset_balance")
        ]

    def __str__(self):
        return f"AssetBalance({self.user_id}, {self.asset_id})"

    @property
    def total(self):
        return self.available + self.reserved


class Deposit(models.Model):
    """One client deposit attempt, across every rail."""

    class Rail(models.TextChoices):
        CRYPTO = "crypto", "Crypto"
        BANK = "bank", "Bank transfer"
        CARD = "card", "Card"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        DETECTED = "detected", "Detected"
        VERIFYING = "verifying", "Verifying"
        VERIFIED = "verified", "Verified"
        CREDITED = "credited", "Credited"
        RESERVED = "reserved", "Reserved"
        AVAILABLE = "available", "Available"
        MANUAL_REVIEW = "manual_review", "Manual review"
        REJECTED = "rejected", "Rejected"
        REVERSED = "reversed", "Reversed"
        EXPIRED = "expired", "Expired"

    #: States in which the client's money has reached our treasury but is not
    #: yet fully available to them.
    CREDITED_STATES = (Status.CREDITED, Status.RESERVED, Status.AVAILABLE)

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="deposits")
    rail = models.CharField(max_length=8, choices=Rail.choices)
    asset = models.ForeignKey(Asset, on_delete=models.PROTECT, related_name="deposits")
    provider = models.ForeignKey(DepositProvider, on_delete=models.PROTECT, related_name="deposits")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    expected_amount = models.DecimalField(max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1])
    received_amount = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], null=True, blank=True
    )
    provider_ref = models.CharField(max_length=128, blank=True, default="")
    idempotency_key = models.CharField(max_length=64)
    source_currency = models.CharField(max_length=16, blank=True, default="")
    source_amount = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], null=True, blank=True
    )
    fx_rate = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], null=True, blank=True
    )
    fee_amount = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], null=True, blank=True
    )
    reference = models.CharField(max_length=32, blank=True, default="")
    instructions = models.JSONField(default=dict, blank=True)
    review_reason = models.CharField(max_length=255, blank=True, default="")
    ledger_entry = models.ForeignKey(
        LedgerEntry, on_delete=models.SET_NULL, null=True, blank=True, related_name="deposits"
    )
    # How much of this deposit currently sits in the client's *reserved* slice.
    # Tracked per deposit so releasing a reserve or reversing a deposit moves
    # exactly this deposit's money, never another deposit's.
    reserved_amount = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO
    )
    credited_at = models.DateTimeField(null=True, blank=True)
    available_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["provider", "provider_ref"],
                condition=models.Q(provider_ref__gt=""),
                name="uniq_deposit_provider_ref",
            ),
            models.UniqueConstraint(fields=["user", "idempotency_key"], name="uniq_deposit_idempotency"),
        ]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["status"]),
            models.Index(fields=["rail", "status"]),
        ]

    def __str__(self):
        return f"Deposit({self.id}, {self.rail}, {self.asset_id}, {self.status})"

    @property
    def is_credited(self):
        return self.status in self.CREDITED_STATES

    @property
    def net_amount(self):
        """The amount actually credited to the client's balance."""
        gross = self.received_amount if self.received_amount is not None else self.expected_amount
        fee = self.fee_amount or ZERO
        return gross - fee


class DepositAddress(models.Model):
    """A crypto deposit address assigned to one client on one network."""

    user = models.ForeignKey(User, on_delete=models.PROTECT, related_name="deposit_addresses")
    network = models.ForeignKey(Network, on_delete=models.PROTECT, related_name="deposit_addresses")
    address = models.CharField(max_length=256)
    memo = models.CharField(max_length=64, blank=True, default="")
    provider_ref = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["network", "address"], name="uniq_deposit_address")
        ]
        indexes = [models.Index(fields=["user", "network", "is_active"])]

    def __str__(self):
        return f"{self.address[:12]}... ({self.user_id})"


class CryptoDeposit(models.Model):
    """Chain-specific details for a crypto deposit."""

    deposit = models.OneToOneField(Deposit, on_delete=models.CASCADE, related_name="crypto")
    network = models.ForeignKey(Network, on_delete=models.PROTECT, related_name="crypto_deposits")
    address = models.CharField(max_length=256, blank=True, default="")
    memo = models.CharField(max_length=64, blank=True, default="")
    txid = models.CharField(max_length=128, blank=True, default="")
    vout = models.CharField(max_length=32, blank=True, default="")
    confirmations = models.PositiveIntegerField(default=0)
    required_confirmations = models.PositiveIntegerField(default=3)
    detected_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["network", "txid", "vout"],
                condition=models.Q(txid__gt=""),
                name="uniq_crypto_deposit_event",
            )
        ]

    def __str__(self):
        return f"CryptoDeposit({self.deposit_id}, {self.txid[:12]})"


class BankDeposit(models.Model):
    """Bank-transfer specific details for a deposit."""

    deposit = models.OneToOneField(Deposit, on_delete=models.CASCADE, related_name="bank")
    provider_account = models.CharField(max_length=64, blank=True, default="")
    provider_reference = models.CharField(max_length=64, blank=True, default="")
    sender_name = models.CharField(max_length=160, blank=True, default="")
    sender_bank = models.CharField(max_length=120, blank=True, default="")
    settled_at = models.DateTimeField(null=True, blank=True)
    reversed_at = models.DateTimeField(null=True, blank=True)
    is_card_funded = models.BooleanField(default=False)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["provider_reference"],
                condition=models.Q(provider_reference__gt=""),
                name="uniq_bank_provider_reference",
            )
        ]

    def __str__(self):
        return f"BankDeposit({self.deposit_id}, {self.provider_account})"


class CardDeposit(models.Model):
    """Card-payment specific details for a deposit.

    Only a token, brand and last four digits are ever stored. The card number
    and CVV live exclusively with the payment provider.
    """

    class DisputeStatus(models.TextChoices):
        NONE = "none", "No dispute"
        OPEN = "open", "Open"
        WON = "won", "Won"
        LOST = "lost", "Lost"

    deposit = models.OneToOneField(Deposit, on_delete=models.CASCADE, related_name="card")
    psp_session_id = models.CharField(max_length=128, blank=True, default="")
    card_brand = models.CharField(max_length=24, blank=True, default="")
    card_last4 = models.CharField(max_length=4, blank=True, default="")
    auth_code = models.CharField(max_length=12, blank=True, default="")
    captured_at = models.DateTimeField(null=True, blank=True)
    settled_at = models.DateTimeField(null=True, blank=True)
    dispute_status = models.CharField(max_length=8, choices=DisputeStatus.choices, default=DisputeStatus.NONE)
    dispute_opened_at = models.DateTimeField(null=True, blank=True)
    dispute_deadline = models.DateTimeField(null=True, blank=True)
    dispute_amount = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], null=True, blank=True
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["psp_session_id"],
                condition=models.Q(psp_session_id__gt=""),
                name="uniq_card_session",
            )
        ]

    def __str__(self):
        return f"CardDeposit({self.deposit_id}, {self.card_brand} {self.card_last4})"


class ProviderEvent(models.Model):
    """Raw inbound provider event, stored before any business logic runs."""

    class Status(models.TextChoices):
        RECEIVED = "received", "Received"
        PROCESSED = "processed", "Processed"
        IGNORED = "ignored", "Ignored"
        FAILED = "failed", "Failed"

    provider = models.ForeignKey(DepositProvider, on_delete=models.PROTECT, related_name="events")
    event_id = models.CharField(max_length=128)
    event_type = models.CharField(max_length=64, blank=True, default="")
    signature_valid = models.BooleanField(default=False)
    payload_sha256 = models.CharField(max_length=64, blank=True, default="")
    raw_payload = models.TextField(blank=True, default="")
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RECEIVED)
    error = models.CharField(max_length=255, blank=True, default="")
    received_at = models.DateTimeField(default=timezone.now)
    processed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-received_at"]
        constraints = [
            models.UniqueConstraint(fields=["provider", "event_id"], name="uniq_provider_event")
        ]

    def __str__(self):
        return f"ProviderEvent({self.provider_id}, {self.event_id})"


class Job(models.Model):
    """DB-backed work queue.

    Chosen over Celery so the platform needs no new infrastructure. A worker
    polls this table; every job is idempotent so a retry is always safe.
    """

    class Kind(models.TextChoices):
        DETECT = "detect", "Detect provider events"
        VERIFY = "verify", "Verify deposit"
        CREDIT = "credit", "Credit deposit"
        RELEASE_RESERVE = "release_reserve", "Release reserved funds"
        RECONCILE = "reconcile", "Reconcile balances"

    class Status(models.TextChoices):
        PENDING = "pending", "Pending"
        RUNNING = "running", "Running"
        DONE = "done", "Done"
        FAILED = "failed", "Failed"
        DEAD = "dead", "Dead letter"

    kind = models.CharField(max_length=24, choices=Kind.choices)
    dedupe_key = models.CharField(max_length=128, blank=True, default="")
    payload = models.JSONField(default=dict, blank=True)
    run_at = models.DateTimeField(default=timezone.now)
    attempts = models.PositiveIntegerField(default=0)
    max_attempts = models.PositiveIntegerField(default=5)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.PENDING)
    last_error = models.CharField(max_length=255, blank=True, default="")
    locked_by = models.CharField(max_length=64, blank=True, default="")
    locked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["run_at", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["kind", "dedupe_key"],
                condition=models.Q(dedupe_key__gt=""),
                name="uniq_job_kind_key",
            )
        ]
        indexes = [models.Index(fields=["status", "run_at"])]

    def __str__(self):
        return f"Job({self.kind}, {self.dedupe_key}, {self.status})"


class ReconciliationRun(models.Model):
    """Evidence that a provider and the ledger agreed (or did not)."""

    provider = models.ForeignKey(
        DepositProvider, on_delete=models.SET_NULL, null=True, blank=True, related_name="reconciliation_runs"
    )
    rail = models.CharField(max_length=8, blank=True, default="")
    period_start = models.DateTimeField()
    period_end = models.DateTimeField()
    provider_total = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO
    )
    ledger_total = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO
    )
    difference = models.DecimalField(
        max_digits=MONEY_DIGITS[0], decimal_places=MONEY_DIGITS[1], default=ZERO
    )
    discrepancy_count = models.PositiveIntegerField(default=0)
    status = models.CharField(max_length=16, default="ok")
    notes = models.TextField(blank=True, default="")
    ran_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-ran_at"]

    def __str__(self):
        return f"ReconciliationRun({self.rail}, {self.status}, {self.difference})"
