from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    DashboardAPIView,
    HealthAPIView,
    InstrumentDetailAPIView,
    InstrumentListAPIView,
    MarketAPIView,
    ProfileAPIView,
    RegisterAPIView,
    SupportTicketAPIView,
)
from .verify import SendVerificationAPIView, VerifyEmailAPIView

urlpatterns = [
    path("instruments/", InstrumentListAPIView.as_view(), name="api_instruments"),
    path("instruments/<str:symbol>/", InstrumentDetailAPIView.as_view(), name="api_instrument_detail"),
    path("auth/register/", RegisterAPIView.as_view(), name="api_register"),
    path("auth/token/", TokenObtainPairView.as_view(), name="token_obtain_pair"),
    path("auth/token/refresh/", TokenRefreshView.as_view(), name="token_refresh"),
    path("profile/", ProfileAPIView.as_view(), name="api_profile"),
    path("dashboard/", DashboardAPIView.as_view(), name="api_dashboard"),
    path("support/", SupportTicketAPIView.as_view(), name="api_support"),
    path("health/", HealthAPIView.as_view(), name="api_health"),
    path("market/", MarketAPIView.as_view(), name="api_market"),

    # email verification
    path("auth/send-verification/", SendVerificationAPIView.as_view(), name="api_send_verification"),
    path("auth/verify/", VerifyEmailAPIView.as_view(), name="api_verify_email"),
]
