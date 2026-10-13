from django.contrib.auth.forms import AuthenticationForm
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils import timezone


class User(AbstractUser):
    class Role(models.TextChoices):
        CLIENT = "client", "Client"
        ADMIN = "admin", "Admin"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.CLIENT)
    is_verified = models.BooleanField(default=False)

    def is_admin(self):
        return self.role == self.Role.ADMIN or self.is_staff

    def __str__(self):
        return self.get_full_name() or self.email or self.username
