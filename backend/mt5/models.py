from django.conf import settings
from django.db import models

class Mt5Credential(models.Model):
    STATUS_CHOICES = [
        ("NEVER", "NEVER"),
        ("PENDING", "PENDING"),
        ("SUCCESS", "SUCCESS"),
        ("FAILED", "FAILED"),
        ("TIMEOUT", "TIMEOUT"),
    ]

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="mt5_credential")
    login = models.CharField(max_length=64)
    server = models.CharField(max_length=128)
    password_enc = models.TextField()

    last_status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="NEVER")
    last_verified_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

from django.conf import settings
from django.db import models
from django.utils import timezone

class Mt5Instance(models.Model):
    PLATFORM_CHOICES = [
        ("LINUX", "LINUX"),
        ("WINDOWS", "WINDOWS"),
    ]

    hostname = models.CharField(max_length=128, unique=True)
    platform = models.CharField(max_length=16, choices=PLATFORM_CHOICES, default="LINUX")
    is_admin = models.BooleanField(default=False)

    # Windows RDP target
    rdp_host = models.CharField(max_length=64, blank=True, default="")  # e.g. 10.50.0.2
    guac_connection_id = models.IntegerField(null=True, blank=True)
    windows_username = models.CharField(max_length=64, blank=True, default="")
    windows_password_enc = models.TextField(blank=True, default="")

    is_leased = models.BooleanField(default=False)
    leased_to = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="mt5_leases",
    )
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    last_seen_at = models.DateTimeField(null=True, blank=True)
