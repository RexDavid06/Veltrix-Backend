import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import IntegrityError, transaction
from django.db.models import Q

from veltrix_app.models import User


class Command(BaseCommand):
    help = (
        "Create the permanent administrator account (role=admin, is_staff, "
        "is_superuser, is_verified, is_active) exactly once. Safe to re-run: "
        "if a complete active admin already exists the command exits "
        "successfully WITHOUT changing the password, permissions, or any "
        "other field. If a matching account exists but is NOT a complete "
        "active admin, the command exits non-zero WITHOUT modifying "
        "anything, so the mismatch gets manual review instead of a silent "
        "promotion."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--username",
            default=os.environ.get("DJANGO_SUPERUSER_USERNAME", ""),
            help="Login username. Defaults to DJANGO_SUPERUSER_USERNAME, then to "
            "the lowercased email (this project's login convention).",
        )
        parser.add_argument(
            "--email",
            default=os.environ.get("DJANGO_SUPERUSER_EMAIL", ""),
            help="Admin email address. Defaults to DJANGO_SUPERUSER_EMAIL.",
        )
        parser.add_argument(
            "--password",
            default=os.environ.get("DJANGO_SUPERUSER_PASSWORD", ""),
            help="Password for a NEW account. Defaults to DJANGO_SUPERUSER_PASSWORD. "
            "Never printed. Ignored (never reset) if the account already exists.",
        )
        parser.add_argument(
            "--first-name",
            default=os.environ.get("DJANGO_SUPERUSER_FIRST_NAME", "Admin"),
            help="First name used only when the account is first created.",
        )
        parser.add_argument(
            "--last-name",
            default=os.environ.get("DJANGO_SUPERUSER_LAST_NAME", "User"),
            help="Last name used only when the account is first created.",
        )

    def handle(self, *args, **options):
        email = (options.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise CommandError(
                "A valid email address is required (pass --email or set DJANGO_SUPERUSER_EMAIL)."
            )

        # Login treats the username as a case-insensitive email (see
        # UserRegistrationSerializer and EmailTokenObtainPairSerializer), so the
        # admin account must follow the same convention to be usable.
        username = (options.get("username") or "").strip().lower() or email

        existing = (
            User.objects.filter(
                Q(username__iexact=username)
                | Q(username__iexact=email)
                | Q(email__iexact=username)
                | Q(email__iexact=email)
            )
            .order_by("pk")
            .first()
        )
        if existing is not None:
            self._handle_existing(existing)
            return

        password = options.get("password") or ""
        if not password:
            raise CommandError(
                "A password is required to create a NEW account "
                "(pass --password or set DJANGO_SUPERUSER_PASSWORD)."
            )
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError("Password rejected: " + " ".join(exc.messages))

        user = User(
            username=username,
            email=email,
            first_name=(options.get("first_name") or "").strip() or "Admin",
            last_name=(options.get("last_name") or "").strip() or "User",
            role=User.Role.ADMIN,
            is_staff=True,
            is_superuser=True,
            is_verified=True,
            is_active=True,
        )
        user.set_password(password)
        try:
            with transaction.atomic():
                user.save()
        except IntegrityError:
            raise CommandError(
                f"Account '{username}' already exists (created concurrently); "
                "no duplicate was created and nothing was changed."
            )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created admin account '{user.username}' "
                "(role=admin, is_staff=True, is_superuser=True, is_verified=True). "
                "The password is not printed."
            )
        )

    def _handle_existing(self, user):
        """Never modify an existing account; only report success or fail.

        A complete, active admin (all required flags set) exits 0 untouched.
        Anything else raises CommandError so the process exits non-zero and
        surfaces the mismatch for manual review - the account's password,
        role, and flags are never changed and it is never promoted here.
        """
        problems = []
        if user.role != User.Role.ADMIN:
            problems.append(f"role is '{user.role}', not 'admin'")
        if not user.is_staff:
            problems.append("is_staff is False")
        if not user.is_superuser:
            problems.append("is_superuser is False")
        if not user.is_verified:
            problems.append("is_verified is False")
        if not user.is_active:
            problems.append("the account is inactive")

        if not problems:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Account '{user.username}' already exists and is a complete "
                    "active admin; nothing was created or changed "
                    "(password and permissions untouched)."
                )
            )
            return

        raise CommandError(
            f"Account '{user.username}' already exists but is not a complete "
            "administrator (" + "; ".join(problems) + "). Nothing was changed: "
            "the password, role, and permission flags were NOT modified and the "
            "account was NOT promoted. Manual review is required before this "
            "command can succeed."
        )
