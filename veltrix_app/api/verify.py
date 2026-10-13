from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView
from django.contrib.auth import get_user_model
from ..utils.email import send_verification_email, verify_token

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
