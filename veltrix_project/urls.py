from __future__ import annotations

from django.urls import include, path
from django.contrib import admin

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/", include("veltrix_app.api.urls")),
    path("", include("veltrix_app.urls")),
]
