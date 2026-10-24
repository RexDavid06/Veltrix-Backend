from __future__ import annotations

from django.contrib import admin

from .models import AuditLog, DemoAccount, Instrument, Position, SupportTicket, Transaction, User, WatchlistItem


class ReadOnlyFinancialRecordsAdmin(admin.ModelAdmin):
    """Allow authorized staff to inspect these records without changing them."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def has_view_permission(self, request, obj=None):
        opts = self.opts
        return request.user.has_perm(
            f"{opts.app_label}.view_{opts.model_name}"
        ) or request.user.has_perm(f"{opts.app_label}.change_{opts.model_name}")


@admin.register(User)
class UserAdmin(admin.ModelAdmin):
    list_display = ("username", "email", "role", "is_staff", "is_active")
    list_filter = ("role", "is_staff", "is_active")
    search_fields = ("username", "email")


@admin.register(DemoAccount)
class DemoAccountAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("user", "currency", "cash_balance")
    readonly_fields = tuple(field.name for field in DemoAccount._meta.fields)


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "name", "asset_class", "price", "change_percent", "is_active")
    search_fields = ("symbol", "name")
    list_filter = ("asset_class", "is_active")


@admin.register(Position)
class PositionAdmin(admin.ModelAdmin):
    list_display = ("user", "instrument", "quantity", "avg_price")


@admin.register(Transaction)
class TransactionAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("user", "symbol", "side", "quantity", "amount", "status", "created_at")
    list_filter = ("side", "status")
    readonly_fields = tuple(field.name for field in Transaction._meta.fields)


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
