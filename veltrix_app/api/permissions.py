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
