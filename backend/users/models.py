from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    """
    Custom user model for GuvFX.

    - Uses email as the unique identifier for login.
    - Keeps 'username' field (required by AbstractUser) but we can de-emphasize it in the UI.
    """

    email = models.EmailField(unique=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["username"]  # required when creating superuser

    def __str__(self):
        return self.email or self.username