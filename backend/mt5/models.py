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
