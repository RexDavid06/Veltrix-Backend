from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError as DjangoValidationError
from django.utils.encoding import force_str
from django.utils.http import urlsafe_base64_decode
from ..utils.email import send_verification_email, verify_token, send_password_reset_email

User = get_user_model()


class SendVerificationAPIView(APIView):
    permission_classes = ()  # handled manually (allow authenticated only)

    def post(self, request):
        user = request.user
        if not user or not user.is_authenticated:
            return Response({"detail": "Authentication required"}, status=status.HTTP_401_UNAUTHORIZED)
        if user.is_verified:
            return Response({"detail": "Already verified"}, status=status.HTTP_400_BAD_REQUEST)
        send_verification_email(user)
        return Response({"detail": "verification_sent"}, status=status.HTTP_202_ACCEPTED)


class VerifyEmailAPIView(APIView):
    permission_classes = ()

    def get(self, request):
        token = request.GET.get("token")
        if not token:
            return Response({"detail": "token_required"}, status=status.HTTP_400_BAD_REQUEST)
        user_id = verify_token(token)
        if not user_id:
            return Response({"detail": "invalid_or_expired"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            user = User.objects.get(id=user_id)
            user.is_verified = True
            user.save()
            return Response({"detail": "verified"})
        except User.DoesNotExist:
            return Response({"detail": "user_not_found"}, status=status.HTTP_404_NOT_FOUND)


class PasswordResetRequestAPIView(APIView):
    """Request a password reset link for an email address.

    Always returns the same response so the endpoint cannot be used to probe
    which email addresses have accounts.
    """

    permission_classes = ()
    GENERIC_DETAIL = "If an account exists for that email, a reset link has been sent."

    def post(self, request):
        raw_email = request.data.get("email")
        email = (raw_email or "").strip().lower()
        if not email:
            return Response({"detail": "email is required"}, status=status.HTTP_400_BAD_REQUEST)

        user = User.objects.filter(email__iexact=email, is_active=True).first()
        if user is not None:
            send_password_reset_email(user)
        return Response({"detail": self.GENERIC_DETAIL}, status=status.HTTP_200_OK)


class PasswordResetConfirmAPIView(APIView):
    """Complete a password reset using the uid + token emailed to the user."""

    permission_classes = ()

    def post(self, request):
        uid = request.data.get("uid")
        token = request.data.get("token")
        new_password = request.data.get("new_password")

        if not uid or not token or not new_password:
            return Response(
                {"detail": "uid, token, and new_password are required"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            user_id = force_str(urlsafe_base64_decode(uid))
            user = User.objects.get(pk=user_id)
        except (TypeError, ValueError, OverflowError, User.DoesNotExist):
            user = None

        if user is None or not default_token_generator.check_token(user, token):
            return Response({"detail": "invalid_or_expired"}, status=status.HTTP_400_BAD_REQUEST)

        try:
            validate_password(new_password, user=user)
        except DjangoValidationError as exc:
            return Response({"new_password": list(exc.messages)}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(new_password)
        user.save(update_fields=["password"])
        return Response({"detail": "password_reset"}, status=status.HTTP_200_OK)
