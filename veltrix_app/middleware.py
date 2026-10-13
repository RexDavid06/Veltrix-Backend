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
            path = request.path
            method = request.method
            if not user or not user.is_authenticated:
                return None

            # Admin area actions
            if path.startswith("/admin") or user.is_staff or user.is_superuser:
                AuditLog.objects.create(actor=user, user=user, action=f"request:{method}", details=path)
                return None

            # Important state changes
            if method in ("POST", "PUT", "DELETE"):
                AuditLog.objects.create(actor=user, user=user, action=f"request:{method}", details=path)
        except Exception:
            # Never raise from middleware
            pass
        return None
