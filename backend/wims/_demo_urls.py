"""Minimal URLconf for the isolated WP-1 demo/test settings."""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/wims/", include("wims.urls")),
]
