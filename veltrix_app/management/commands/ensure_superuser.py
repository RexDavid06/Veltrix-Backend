import os

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q

from veltrix_app.models import User

CONFIRM_PHRASE = "RESET_EXISTING_ADMIN"
REQUIRED_FIELDS = (
    "username",
    "email",
    "is_active",
    "is_staff",
    "is_superuser",
    "role",
    "is_verified",
)


class Command(BaseCommand):
    help = (
        "Reset the single existing active production administrator account "
        "in place: set its username, email, and password from the "
        "DJANGO_SUPERUSER_* environment variables and restore the full admin "
        "privilege set (role=admin, is_staff, is_superuser, is_verified, "
        "is_active). Requires --confirm RESET_EXISTING_ADMIN; any other "
        "value aborts before any database access. Safe by design: refuses "
        "to run unless exactly one active superuser exists, never creates "
        "an account (not even as a fallback), never touches any other "
        "account, never prints the password, and aborts without writing "
        "when the requested username or email is already held by another "
        "account."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--confirm",
            default="",
            help="Must be exactly RESET_EXISTING_ADMIN. Anything else aborts "
            "before any database access.",
        )
        parser.add_argument(
            "--username",
            default=os.environ.get("DJANGO_SUPERUSER_USERNAME", ""),
            help="New username for the existing admin account. Defaults to "
            "DJANGO_SUPERUSER_USERNAME.",
        )
        parser.add_argument(
            "--email",
            default=os.environ.get("DJANGO_SUPERUSER_EMAIL", ""),
            help="New email for the existing admin account. Defaults to "
            "DJANGO_SUPERUSER_EMAIL.",
        )

    def handle(self, *args, **options):
        # Guard 1: explicit confirmation, checked before ANY database access.
        confirm = (options.get("confirm") or "").strip()
        if confirm != CONFIRM_PHRASE:
            raise CommandError(
                "Confirmation phrase missing or incorrect. Nothing was "
                "changed: pass --confirm RESET_EXISTING_ADMIN to proceed."
            )

        # Guard 2: the configured user model must expose every field this
        # command relies on, including an 'admin' role choice. Fail clearly
        # before writing instead of crashing mid-update on a schema surprise.
        self._verify_model_supports_reset(User)

        # Guard 3: validate the new identity values BEFORE touching any
        # account. Normalization matches the project's convention (trimmed,
        # lowercased).
        username = (options.get("username") or "").strip().lower()
        if not username:
            raise CommandError(
                "A username is required (pass --username or set "
                "DJANGO_SUPERUSER_USERNAME)."
            )

        email = (options.get("email") or "").strip().lower()
        if not email:
            raise CommandError(
                "An email address is required (pass --email or set "
                "DJANGO_SUPERUSER_EMAIL)."
            )
        try:
            validate_email(email)
        except ValidationError as exc:
            raise CommandError(
                "Email rejected by Django's email validators: "
                + " ".join(exc.messages)
            )

        # Guard 4: the password is read ONLY from the environment (never a
        # CLI argument) so it cannot appear in the workflow command line, the
        # process list, or any log. Validated before any account is touched.
        password = os.environ.get("DJANGO_SUPERUSER_PASSWORD") or ""
        if not password:
            raise CommandError(
                "DJANGO_SUPERUSER_PASSWORD is not set. Nothing was changed."
            )
        try:
            validate_password(password)
        except ValidationError as exc:
            raise CommandError(
                "Admin password rejected by Django's password validators: "
                + " ".join(exc.messages)
            )

        # Guard 5: census. Proceed only when exactly one eligible account
        # (an ACTIVE superuser) exists. Zero or multiple matches abort with
        # no writes at all - this command never guesses which account to
        # change and never creates one as a fallback.
        if User.objects.filter(is_superuser=True, is_active=True).count() != 1:
            raise CommandError(
                "Expected exactly one active superuser in the database but "
                "found a different number. Nothing was changed: manual "
                "review is required before the reset can proceed."
            )

        # All checks passed; perform the single-account update atomically.
        with transaction.atomic():
            # Lock the rows and recheck the precondition so a concurrent
            # change cannot slip between the census and the write.
            # (select_for_update is a no-op on backends that do not support
            # SELECT ... FOR UPDATE, such as SQLite.)
            locked = list(
                User.objects.select_for_update().filter(
                    is_superuser=True, is_active=True
                )
            )
            if len(locked) != 1:
                raise CommandError(
                    "The set of active superusers changed while resetting. "
                    "Nothing was changed."
                )
            target = locked[0]

            # Guard 6: the new username/email must not belong to any OTHER
            # account (case-insensitive on both fields).
            conflict = (
                User.objects.filter(
                    Q(username__iexact=username)
                    | Q(username__iexact=email)
                    | Q(email__iexact=username)
                    | Q(email__iexact=email)
                )
                .exclude(pk=target.pk)
                .exists()
            )
            if conflict:
                raise CommandError(
                    "The requested username or email is already held by "
                    "another account. Nothing was changed."
                )

            # Update ONLY this account: identity, password, and a full set
            # of admin privileges. No other row is read for writing and no
            # user is ever created here.
            target.username = username
            target.email = email
            target.set_password(password)
            target.is_active = True
            target.is_staff = True
            target.is_superuser = True
            target.role = User.Role.ADMIN.value
            target.is_verified = True
            target.save()

        self.stdout.write(
            self.style.SUCCESS(
                f"Reset admin account '{target.username}' ({target.email}): "
                "is_active=True, is_staff=True, is_superuser=True, "
                f"role='{target.role}', is_verified=True. No account was "
                "created and no other account was modified; the password "
                "was set and is not printed."
            )
        )

    def _verify_model_supports_reset(self, User):
        """Fail clearly (before any write) if the user model lacks a field
        or the role choice this command depends on."""
        missing = []
        for field_name in REQUIRED_FIELDS:
            try:
                User._meta.get_field(field_name)
            except FieldDoesNotExist:
                missing.append(field_name)
        if missing:
            raise CommandError(
                "The configured user model is missing required field(s): "
                + ", ".join(missing)
                + ". Nothing was changed."
            )

        role_choice = getattr(getattr(User, "Role", None), "ADMIN", None)
        role_value = getattr(role_choice, "value", role_choice)
        allowed_roles = {value for value, _ in User._meta.get_field("role").choices}
        if role_value is None or role_value not in allowed_roles:
            raise CommandError(
                "The configured user model does not support an 'admin' role "
                "choice. Nothing was changed."
            )
