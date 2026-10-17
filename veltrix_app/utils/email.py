from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core import signing
from django.core.mail import send_mail
from django.urls import reverse
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode


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
    verify_url = f"{settings.FRONTEND_URL}/?verify_token={token}"
    subject = "Verify your VELTRIX account"
    body = f"Hi {user.get_full_name() or user.email},\n\nPlease verify your email by visiting the following link:\n{verify_url}\n\nIf you did not request this, ignore this email.\n\nThanks,\nVELTRIX team"
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)


def make_password_reset_uid(user) -> str:
    return urlsafe_base64_encode(force_bytes(user.pk))


def make_password_reset_token(user) -> str:
    """Standard Django reset token.

    It automatically becomes invalid once the password (or last login) changes,
    which makes it effectively single-use without extra storage.
    """
    return default_token_generator.make_token(user)


def send_password_reset_email(user):
    uid = make_password_reset_uid(user)
    token = make_password_reset_token(user)
    reset_url = f"{settings.FRONTEND_URL}/?reset_uid={uid}&reset_token={token}"
    subject = "Reset your VELTRIX password"
    body = (
        f"Hi {user.get_full_name() or user.email},\n\n"
        f"We received a request to reset your VELTRIX password. "
        f"Use the link below to choose a new one:\n{reset_url}\n\n"
        f"If you did not request this, you can safely ignore this email; "
        f"your password will not change.\n\nThanks,\nVELTRIX team"
    )
    send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [user.email], fail_silently=True)
