import getpass

from django.core.management.base import BaseCommand, CommandError

from veltrix_app.models import User


class Command(BaseCommand):
    help = (
        "Create or update an admin user that can sign in to the VELTRIX app "
        "and open the admin dashboard (role=admin, is_staff, is_superuser)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--email",
            default="",
            help="Login email for the admin account. Also used as the username, "
            "because login treats the username as a case-insensitive email.",
        )
        parser.add_argument(
            "--password",
            default="",
            help="Password for the admin account. If omitted you will be prompted.",
        )

    def handle(self, *args, **options):
        email = (options.get("email") or "").strip().lower()
        if not email:
            email = input("Email: ").strip().lower()
        if not email or "@" not in email:
            raise CommandError("A valid email address is required.")

        password = options.get("password") or ""
        if not password:
            password = getpass.getpass("Password: ")
        if len(password) < 8:
            raise CommandError("Password must be at least 8 characters.")

        user, created = User.objects.get_or_create(
            username=email,
            defaults={"email": email, "first_name": "Admin", "last_name": "User"},
        )
        user.email = email
        user.role = User.Role.ADMIN
        user.is_staff = True
        user.is_superuser = True
        user.is_verified = True
        user.is_active = True
        user.set_password(password)
        user.save()

        state = "created" if created else "updated"
        self.stdout.write(
            self.style.SUCCESS(
                f"Admin account {email} {state}. "
                "Sign in on the site with that email and password to reach the admin dashboard."
            )
        )
