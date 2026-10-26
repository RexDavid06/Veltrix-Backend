from __future__ import annotations

from django.contrib import admin

from .models import (
    Asset,
    AuditLog,
    ChainScanCursor,
    ChainTransfer,
    CryptoDeposit,
    DemoAccount,
    Deposit,
    DepositAddress,
    DepositProvider,
    Instrument,
    Network,
    Position,
    SupportTicket,
    Transaction,
    User,
    WatchlistItem,
)


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


@admin.register(Asset)
class AssetAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("code", "display_name", "decimals", "is_active")
    search_fields = ("code", "display_name")
    readonly_fields = tuple(field.name for field in Asset._meta.fields)


@admin.register(DepositProvider)
class DepositProviderAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("code", "display_name", "rail", "is_active")
    list_filter = ("rail", "is_active")
    readonly_fields = tuple(field.name for field in DepositProvider._meta.fields)


@admin.register(Network)
class NetworkAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("chain", "asset", "token_standard", "is_testnet", "is_active")
    list_filter = ("chain", "is_testnet", "is_active")
    readonly_fields = tuple(field.name for field in Network._meta.fields)


@admin.register(Deposit)
class DepositAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("reference", "user", "asset", "expected_amount", "status", "rail", "created_at")
    list_filter = ("status", "rail", "asset")
    search_fields = ("reference", "user__username", "provider_ref")
    readonly_fields = tuple(field.name for field in Deposit._meta.fields)


@admin.register(CryptoDeposit)
class CryptoDepositAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("deposit", "network", "txid", "confirmations", "required_confirmations")
    list_filter = ("network",)
    search_fields = ("txid", "address")
    readonly_fields = tuple(field.name for field in CryptoDeposit._meta.fields)


@admin.register(DepositAddress)
class DepositAddressAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("address", "user", "network", "deposit", "is_active")
    list_filter = ("network", "is_active")
    search_fields = ("address", "provider_ref")
    readonly_fields = tuple(field.name for field in DepositAddress._meta.fields)


@admin.register(ChainScanCursor)
class ChainScanCursorAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("network", "timestamp", "block_number", "window_end_timestamp", "updated_at")
    readonly_fields = tuple(field.name for field in ChainScanCursor._meta.fields)


@admin.register(ChainTransfer)
class ChainTransferAdmin(ReadOnlyFinancialRecordsAdmin):
    list_display = ("txid", "event_index", "network", "recipient_address", "amount", "status", "block_number")
    list_filter = ("status", "network")
    search_fields = ("txid", "sender_address", "recipient_address")
    readonly_fields = tuple(field.name for field in ChainTransfer._meta.fields)
