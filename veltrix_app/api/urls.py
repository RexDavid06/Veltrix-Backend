from django.urls import include, path
from rest_framework_simplejwt.views import TokenBlacklistView, TokenRefreshView

from .views import (
    AssignConversationAPIView,
    ConversationDetailAPIView,
    ConversationMessagesAPIView,
    ConversationsListCreateAPIView,
    DashboardAPIView,
    EmailTokenObtainPairView,
    HealthAPIView,
    InstrumentDetailAPIView,
    InstrumentListAPIView,
    MarketAPIView,
    MarkReadAPIView,
    OrderAPIView,
    ProfileAPIView,
    RegisterAPIView,
    ReopenConversationAPIView,
    ResolveConversationAPIView,
    SupportTicketAPIView,
    TopUpAPIView,
    WatchlistAPIView,
    WatchlistDetailAPIView,
)
from .deposits import (
    BankDepositCreateAPIView,
    DepositConfigAPIView,
    DepositDetailAPIView,
    DepositListCreateAPIView,
    DepositQuoteConfirmAPIView,
    DepositTxidAPIView,
    NowPaymentsWebhookAPIView,
)
from .verify import (
    PasswordResetConfirmAPIView,
    PasswordResetRequestAPIView,
    SendVerificationAPIView,
    VerifyEmailAPIView,
)

urlpatterns = [
    path("instruments/", InstrumentListAPIView.as_view(), name="api_instruments"),
    path("instruments/<str:symbol>/", InstrumentDetailAPIView.as_view(), name="api_instrument_detail"),
    path("auth/register/", RegisterAPIView.as_view(), name="api_register"),
    path("auth/token/", EmailTokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("auth/logout/", TokenBlacklistView.as_view(), name="token_blacklist"),
    path("profile/", ProfileAPIView.as_view(), name="api_profile"),
    path("dashboard/", DashboardAPIView.as_view(), name="api_dashboard"),
    path("support/", SupportTicketAPIView.as_view(), name="api_support"),
    path("health/", HealthAPIView.as_view(), name="api_health"),
    path("market/", MarketAPIView.as_view(), name="api_market"),

    path("deposits/", DepositListCreateAPIView.as_view(), name="api_deposits"),
    path("deposits/config/", DepositConfigAPIView.as_view(), name="api_deposit_config"),
    path("deposits/bank/", BankDepositCreateAPIView.as_view(), name="api_bank_deposit_create"),
    path("deposits/<int:pk>/", DepositDetailAPIView.as_view(), name="api_deposit_detail"),
    path("deposits/<int:pk>/txid/", DepositTxidAPIView.as_view(), name="api_deposit_txid"),
    path("deposits/<int:pk>/confirm/", DepositQuoteConfirmAPIView.as_view(), name="api_deposit_quote_confirm"),
    path("webhooks/nowpayments/", NowPaymentsWebhookAPIView.as_view(), name="api_nowpayments_webhook"),

    # Demo trading, funding, and watchlist
    path("watchlist/", WatchlistAPIView.as_view(), name="api_watchlist"),
    path("watchlist/<str:symbol>/", WatchlistDetailAPIView.as_view(), name="api_watchlist_detail"),
    path("orders/", OrderAPIView.as_view(), name="api_orders"),
    path("portfolio/topup/", TopUpAPIView.as_view(), name="api_portfolio_topup"),

    # Support conversations / chat
    path("conversations/", ConversationsListCreateAPIView.as_view(), name="api_conversations"),
    path("conversations/<int:pk>/", ConversationDetailAPIView.as_view(), name="api_conversation_detail"),
    path("conversations/<int:pk>/messages/", ConversationMessagesAPIView.as_view(), name="api_conversation_messages"),
    path("conversations/<int:pk>/mark-read/", MarkReadAPIView.as_view(), name="api_conversation_mark_read"),
    path("conversations/<int:pk>/assign/", AssignConversationAPIView.as_view(), name="api_conversation_assign"),
    path("conversations/<int:pk>/resolve/", ResolveConversationAPIView.as_view(), name="api_conversation_resolve"),
    path("conversations/<int:pk>/reopen/", ReopenConversationAPIView.as_view(), name="api_conversation_reopen"),

    # email verification
    path("auth/send-verification/", SendVerificationAPIView.as_view(), name="api_send_verification"),
    path("auth/verify/", VerifyEmailAPIView.as_view(), name="api_verify_email"),

    # password reset
    path("auth/password-reset/", PasswordResetRequestAPIView.as_view(), name="api_password_reset"),
    path("auth/password-reset/confirm/", PasswordResetConfirmAPIView.as_view(), name="api_password_reset_confirm"),

    # Admin dashboard API (every endpoint is guarded by IsPlatformAdmin)
    path("admin/", include("veltrix_app.api.admin_urls")),
]
