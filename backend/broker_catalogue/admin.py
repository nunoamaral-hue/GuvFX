from django.contrib import admin

from broker_catalogue.models import CatalogueArtefact, CatalogueVersion


@admin.register(CatalogueVersion)
class CatalogueVersionAdmin(admin.ModelAdmin):
    list_display = ("label", "status", "manifest_sha256", "activated_at", "created_at")
    list_filter = ("status",)
    readonly_fields = ("manifest_sha256", "created_at", "activated_at", "retired_at")


@admin.register(CatalogueArtefact)
class CatalogueArtefactAdmin(admin.ModelAdmin):
    list_display = ("broker_id", "version", "sha256", "size_bytes", "sanitisation_result",
                    "certification_result", "approval_id")
    list_filter = ("broker_id", "version")
    readonly_fields = ("created_at",)
