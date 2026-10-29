from __future__ import annotations

from django.urls import include, path
from django.contrib import admin
from django.http import JsonResponse


def root_status(request):
    """Give the Render service root a small, useful status response."""
    return JsonResponse({"status": "ok", "service": "veltrix-backend", "health": "/api/health/"})

urlpatterns = [
    path("", root_status, name="root_status"),
    path("admin/", admin.site.urls),
    path("api/", include("veltrix_app.api.urls")),
    path("", include("veltrix_app.urls")),
]
