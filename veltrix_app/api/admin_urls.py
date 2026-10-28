from django.urls import path

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
]

from .admin_views import AdminDepositsListView, AdminDepositActionView, AdminUserDepositsView

urlpatterns += [
    path("deposits/", AdminDepositsListView.as_view(), name="api_admin_deposits_list"),
    path("deposits/<int:pk>/action/", AdminDepositActionView.as_view(), name="api_admin_deposit_action"),
    path("users/<int:pk>/deposits/", AdminUserDepositsView.as_view(), name="api_admin_user_deposits"),
]
