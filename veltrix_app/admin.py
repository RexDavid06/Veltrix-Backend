from __future__ import annotations

from django.contrib import admin

from .models import AuditLog, DemoAccount, Instrument, Position, SupportTicket, Transaction, User, WatchlistItem


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "email", "role", "is_staff", "is_active")
    list_filter = ("role", "is_staff", "is_active")
    search_fields = ("username", "email")


@admin.register(DemoAccount)
class DemoAccountAdmin(admin.ModelAdmin):
    list_display = ("user", "currency", "cash_balance")


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "asset_class", "price", "change_percent", "is_active")
    search_fields = ("symbol", "name")
    list_filter = ("asset_class", "is_active")


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("user", "instrument", "quantity", "avg_price")


@admin.register(Transaction)
class TransactionAdmin(admin.ModelAdmin):
    list_display = ("user", "symbol", "side", "quantity", "amount", "status", "created_at")
    list_filter = ("side", "status")


@admin.register(WatchlistItem)
class WatchlistItemAdmin(admin.ModelAdmin):
    list_display = ("user", "instrument")


@admin.register(SupportTicket)
class SupportTicketAdmin(admin.ModelAdmin):
    list_display = ("user", "subject", "status", "created_at")
    list_filter = ("status",)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("action", "user", "actor", "created_at")
    search_fields = ("action", "details")
