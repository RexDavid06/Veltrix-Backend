from django.urls import path

from .admin_deposits import (
    AdminBankApproveView,
    AdminBankRejectView,
    AdminChainTransferApproveView,
    AdminChainTransferAssignView,
    AdminChainTransferFlagView,
    AdminChainTransferListView,
    AdminDepositDetailView,
    AdminDepositListView,
    AdminDiscrepancyResolveView,
    AdminReconciliationView,
)
from .admin_views import (
    AdminAuditLogListView,
    AdminInstrumentDetailView,
    AdminInstrumentListView,
    AdminStatsView,
    AdminTransactionListView,
    AdminUserDetailView,
    AdminUserListView,
)

urlpatterns = [
    path("stats/", AdminStatsView.as_view(), name="api_admin_stats"),
    path("users/", AdminUserListView.as_view(), name="api_admin_users"),
    path("users/<int:pk>/", AdminUserDetailView.as_view(), name="api_admin_user_detail"),
    path("transactions/", AdminTransactionListView.as_view(), name="api_admin_transactions"),
    path("instruments/", AdminInstrumentListView.as_view(), name="api_admin_instruments"),
    path("instruments/<int:pk>/", AdminInstrumentDetailView.as_view(), name="api_admin_instrument_detail"),
    path("audit/", AdminAuditLogListView.as_view(), name="api_admin_audit"),
    path("deposits/<int:pk>/", AdminDepositDetailView.as_view(), name="api_admin_deposit_detail"),
    path("chain-transfers/", AdminChainTransferListView.as_view(), name="api_admin_chain_transfers"),
    path("chain-transfers/<int:pk>/assign/", AdminChainTransferAssignView.as_view(), name="api_admin_chain_transfer_assign"),
    path("chain-transfers/<int:pk>/approve/", AdminChainTransferApproveView.as_view(), name="api_admin_chain_transfer_approve"),
    path("chain-transfers/<int:pk>/flag/", AdminChainTransferFlagView.as_view(), name="api_admin_chain_transfer_flag"),
    path("bank-deposits/<int:pk>/approve/", AdminBankApproveView.as_view(), name="api_admin_bank_approve"),
    path("bank-deposits/<int:pk>/reject/", AdminBankRejectView.as_view(), name="api_admin_bank_reject"),
    path("reconciliation/", AdminReconciliationView.as_view(), name="api_admin_reconciliation"),
    path("reconciliation/discrepancies/<int:pk>/resolve/", AdminDiscrepancyResolveView.as_view(), name="api_admin_discrepancy_resolve"),
]
