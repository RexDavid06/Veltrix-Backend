from django.core import signing
from django.core.mail import send_mail
from django.urls import reverse
from django.conf import settings


def make_verification_token(user):
    payload = {"user_id": user.id}
    token = signing.dumps(payload, salt="email-confirm")
    return token


def verify_token(token, max_age: int = 60 * 60 * 24):
    try:
        data = signing.loads(token, salt="email-confirm", max_age=max_age)
        return data.get("user_id")
    except signing.BadSignature:
        return None


def send_verification_email(user):
    token = make_verification_token(user)
    verify_url = f"{settings.FRONTEND_URL}/verify-email/?token={token}"
    subject = "Verify your VELTRIX account"
    body = f"Hi {user.get_full_name() or user.email},\n\nPlease verify your email by visiting the following link:\n{verify_url}\n\nIf you did not request this, ignore this email.\n\nThanks,\nVELTRIX team"
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)
