from rest_framework import permissions


class IsSupportStaff(permissions.BasePermission):
    """
    Permission check for support staff / admin users.
    """

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and (getattr(request.user, "is_staff", False) or (hasattr(request.user, "is_admin") and request.user.is_admin()))
        )


class IsPlatformAdmin(permissions.BasePermission):
    """Server-side gate for every /api/admin/ endpoint.

    Mirrors IsSupportStaff but is intentionally a separate class so admin
    authorization decisions are explicit and cannot be weakened by changing
    the support permission. The account must also be active.
    """

    def has_permission(self, request, view):
        user = request.user
        if not (user and user.is_authenticated and user.is_active):
            return False
        return bool(getattr(user, "is_staff", False) or (hasattr(user, "is_admin") and user.is_admin()))
