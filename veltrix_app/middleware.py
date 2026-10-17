from __future__ import annotations

from django.utils.deprecation import MiddlewareMixin
from .models import AuditLog


class AuditLogMiddleware(MiddlewareMixin):
    """Simple audit logging middleware.

    - Logs admin actions and authenticated user requests for admin paths.
    - Non-admin requests are logged only for important paths (POST/PUT/DELETE).
    """

    def process_request(self, request):
        try:
            user = getattr(request, "user", None)
            if not user or not user.is_authenticated:
                return None

            path = request.path
            method = request.method
            is_write = method in ("POST", "PUT", "PATCH", "DELETE")
            is_admin_area = path.startswith("/admin")

            # Only state-changing requests and django-admin page loads are worth
            # persisting. Logging every authenticated GET (previously every
            # request for any staff user, across the whole app) grows the table
            # without bound. Static assets are short-circuited by WhiteNoise
            # before they reach this middleware.
            if not (is_write or is_admin_area):
                return None

            AuditLog.objects.create(actor=user, user=user, action=f"request:{method}", details=path)
        except Exception:
            # Never raise from middleware
            pass
        return None
