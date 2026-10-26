"""Lease-backed locks and heartbeats for scheduled funding jobs."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from veltrix_app.models import ScheduledJob


class JobLocked(RuntimeError):
    """Another worker still holds a live lease for this job."""


def _lease_seconds(lease_seconds=None) -> int:
    seconds = lease_seconds
    if seconds is None:
        seconds = getattr(settings, "TRON_JOB_LOCK_SECONDS", 900)
    return max(60, int(seconds))


@contextmanager
def job_lock(name: str, lease_seconds=None):
    """Run a command under a database lease with success/failure heartbeats.

    The lease is intentionally bounded so a crashed process cannot block the
    scheduler forever. A job that outlives its lease must be treated as an
    operational incident, not as a reason to overlap silently.
    """
    owner = uuid.uuid4().hex
    now = timezone.now()
    expires_at = now + timedelta(seconds=_lease_seconds(lease_seconds))
    with transaction.atomic():
        job, _ = ScheduledJob.objects.get_or_create(name=name)
        job = ScheduledJob.objects.select_for_update().get(pk=job.pk)
        if job.lock_owner and job.lock_expires_at and job.lock_expires_at > now:
            raise JobLocked(
                f"{name} is already running until {job.lock_expires_at.isoformat()}"
            )
        job.lock_owner = owner
        job.lock_expires_at = expires_at
        job.last_started_at = now
        job.updated_at = now
        job.save(
            update_fields=[
                "lock_owner",
                "lock_expires_at",
                "last_started_at",
                "updated_at",
            ]
        )

    try:
        yield job
    except Exception as exc:
        _record_failure(name, owner, exc)
        raise
    else:
        _record_success(name, owner)
    finally:
        _release(name, owner)


def _record_success(name: str, owner: str) -> None:
    with transaction.atomic():
        job = (
            ScheduledJob.objects.select_for_update()
            .filter(name=name, lock_owner=owner)
            .first()
        )
        if not job:
            return
        job.last_success_at = timezone.now()
        job.consecutive_failures = 0
        job.last_error = ""
        job.updated_at = job.last_success_at
        job.save(
            update_fields=[
                "last_success_at",
                "consecutive_failures",
                "last_error",
                "updated_at",
            ]
        )


def _record_failure(name: str, owner: str, exc: Exception) -> None:
    with transaction.atomic():
        job = (
            ScheduledJob.objects.select_for_update()
            .filter(name=name, lock_owner=owner)
            .first()
        )
        if not job:
            return
        job.last_failure_at = timezone.now()
        job.consecutive_failures += 1
        job.last_error = str(exc)[:500]
        job.updated_at = job.last_failure_at
        job.save(
            update_fields=[
                "last_failure_at",
                "consecutive_failures",
                "last_error",
                "updated_at",
            ]
        )


def _release(name: str, owner: str) -> None:
    with transaction.atomic():
        job = (
            ScheduledJob.objects.select_for_update()
            .filter(name=name, lock_owner=owner)
            .first()
        )
        if not job:
            return
        job.lock_owner = ""
        job.lock_expires_at = None
        job.updated_at = timezone.now()
        job.save(update_fields=["lock_owner", "lock_expires_at", "updated_at"])
