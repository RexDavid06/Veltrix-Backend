from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth import login, logout
from django.contrib.auth.views import LoginView
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from .forms import RegistrationForm, SupportForm
from .models import AuditLog, DemoAccount, Instrument, Position, SupportTicket, Transaction, User
from .services.market import fetch_market_snapshot


@login_required(login_url="/login/")
def dashboard_view(request: HttpRequest) -> HttpResponse:
    account = getattr(request.user, "demo_account", None)
    positions = Position.objects.filter(user=request.user).select_related("instrument")
    transactions = Transaction.objects.filter(user=request.user).order_by("-created_at")[:10]
    return render(
        request,
        "veltrix_app/dashboard.html",
        {
            "account": account,
            "positions": positions,
            "transactions": transactions,
            "is_admin": request.user.is_admin(),
        },
    )


@login_required(login_url="/login/")
def market_view(request: HttpRequest) -> HttpResponse:
    quotes = fetch_market_snapshot()
    return render(request, "veltrix_app/markets.html", {"quotes": quotes})


@login_required(login_url="/login/")
def portfolio_view(request: HttpRequest) -> HttpResponse:
    account = getattr(request.user, "demo_account", None)
    positions = Position.objects.filter(user=request.user).select_related("instrument")
    return render(request, "veltrix_app/portfolio.html", {"account": account, "positions": positions})


@login_required(login_url="/login/")
def support_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = SupportForm(request.POST)
        if form.is_valid():
            SupportTicket.objects.create(
                user=request.user,
                subject=form.cleaned_data["subject"],
                message=form.cleaned_data["message"],
            )
            AuditLog.objects.create(user=request.user, action="support_ticket_created", details=form.cleaned_data["subject"])
            return redirect("support")
    else:
        form = SupportForm()
    tickets = SupportTicket.objects.filter(user=request.user).order_by("-created_at")
    return render(request, "veltrix_app/support.html", {"form": form, "tickets": tickets})


@login_required(login_url="/login/")
def admin_portal_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_admin():
        return HttpResponse("Forbidden", status=403)
    open_tickets = SupportTicket.objects.filter(status="open").select_related("user")
    users = User.objects.order_by("-date_joined")[:20]
    return render(request, "veltrix_app/admin.html", {"open_tickets": open_tickets, "users": users})


@require_http_methods(["GET"])
def health_check(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok", "database": "up", "mode": "simulation"})


@require_http_methods(["GET"])
def instrument_api(request: HttpRequest) -> JsonResponse:
    q = (request.GET.get("q") or "").strip().lower()
    asset = (request.GET.get("asset") or "").strip().lower()
    qs = Instrument.objects.filter(is_active=True)
    if q:
        qs = qs.filter(name__icontains=q) | qs.filter(symbol__icontains=q)
    if asset:
        qs = qs.filter(asset_class__iexact=asset)
    payload = {
        "data": [
            {
                "symbol": instrument.symbol,
                "name": instrument.name,
                "asset_class": instrument.asset_class,
                "currency": instrument.currency,
                "price": float(instrument.price),
                "change_percent": float(instrument.change_percent),
                "note": "Illustrative demo price — not live market data.",
            }
            for instrument in qs.order_by("asset_class", "symbol")
        ],
        "disclaimer": "All prices are illustrative demo data and are not live market data.",
    }
    return JsonResponse(payload)


def register_view(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        form = RegistrationForm(request.POST)
        if form.is_valid():
            user = form.save()
            DemoAccount.objects.get_or_create(user=user, defaults={"currency": "USD", "cash_balance": Decimal("10000.00")})
            login(request, user)
            return redirect("dashboard")
    else:
        form = RegistrationForm()
    return render(request, "registration/register.html", {"form": form})


class CustomLoginView(LoginView):
    template_name = "registration/login.html"
    redirect_authenticated_user = True

    def form_valid(self, form):
        response = super().form_valid(form)
        return response


def logout_view(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("login")
