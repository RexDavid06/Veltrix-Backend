from __future__ import annotations

from django.urls import path

from .views import (
    admin_portal_view,
    dashboard_view,
    health_check,
    logout_view,
    market_view,
    portfolio_view,
    register_view,
    support_view,
)
from django.contrib.auth import views as auth_views
from .views import CustomLoginView

urlpatterns = [
    path("login/", CustomLoginView.as_view(), name="login"),
    path("logout/", logout_view, name="logout"),
    path("register/", register_view, name="register"),
    path("dashboard/", dashboard_view, name="dashboard"),
    path("markets/", market_view, name="markets"),
    path("portfolio/", portfolio_view, name="portfolio"),
    path("support/", support_view, name="support"),
    path("admin-portal/", admin_portal_view, name="admin_portal"),
    path("health/", health_check, name="health"),
]
