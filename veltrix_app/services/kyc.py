"""Identity verification for the funding rails.

``User.is_verified`` means "email confirmed" and is set by the email
verification flow. It says nothing about identity, so it must not be used as a
gate for real money. ``KycProfile`` is the separate record that carries the
identity-verification state, and it is what the deposit pipeline checks.
"""

from __future__ import annotations

from django.utils import timezone

from veltrix_app.models import AuditLog, KycProfile, User


def ensure_profile(user: User) -> KycProfile:
    """Return the client's KYC profile, creating a blank one if needed."""
    profile, _ = KycProfile.objects.get_or_create(user=user)
    return profile


def is_approved(user: User) -> bool:
    return ensure_profile(user).is_approved


def approve_profile(
    profile: KycProfile,
    *,
    actor: User | None = None,
    tier: str = KycProfile.Tier.TIER_1,
    notes: str = "",
) -> KycProfile:
    """Approve a client's identity verification."""
    profile.status = KycProfile.Status.APPROVED
    profile.tier = tier
    profile.verified_at = timezone.now()
    profile.reviewed_by = actor
    if notes:
        profile.notes = notes
    profile.updated_at = timezone.now()
    profile.save(
        update_fields=["status", "tier", "verified_at", "reviewed_by", "notes", "updated_at"]
    )
    AuditLog.objects.create(
        user=profile.user,
        actor=actor,
        action="kyc.approved",
        details=f"profile={profile.id} tier={tier}",
    )
    return profile


def reject_profile(profile: KycProfile, *, actor: User | None = None, reason: str = "") -> KycProfile:
    """Reject a client's identity verification."""
    profile.status = KycProfile.Status.REJECTED
    profile.reviewed_by = actor
    profile.notes = reason
    profile.updated_at = timezone.now()
    profile.save(update_fields=["status", "reviewed_by", "notes", "updated_at"])
    AuditLog.objects.create(
        user=profile.user,
        actor=actor,
        action="kyc.rejected",
        details=f"profile={profile.id} reason={reason}",
    )
    return profile


def status_payload(user: User) -> dict:
    """The client-facing summary of their verification state."""
    profile = ensure_profile(user)
    return {
        "status": profile.status,
        "tier": profile.tier,
        "verified_at": profile.verified_at,
        "full_name": profile.full_name,
        "phone": profile.phone,
        "country": profile.country,
        "can_deposit": profile.is_approved,
    }
