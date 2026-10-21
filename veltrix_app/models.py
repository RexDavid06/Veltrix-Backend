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
