from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.forms import AuthenticationForm
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render


def login_index(request: HttpRequest) -> HttpResponse:
    return render(request, "registration/login.html", {"form": AuthenticationForm()})
