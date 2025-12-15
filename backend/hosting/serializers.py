from rest_framework import serializers

from .models import (
    HostingProvider,
    VpsPlan,
    VpsInstance,
    Mt5Instance,
    UserHostingSubscription,
    HostingRequest,
)


class HostingProviderSerializer(serializers.ModelSerializer):
    class Meta:
        model = HostingProvider
        fields = [
            "id",
            "name",
            "api_type",
            "api_base_url",
            "is_active",
        ]


class VpsPlanSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source="provider.name", read_only=True)

    class Meta:
        model = VpsPlan
        fields = [
            "id",
            "name",
            "description",
            "provider",
            "provider_name",
            "cpu_cores",
            "memory_mb",
            "disk_gb",
            "monthly_price_usd",
            "code",
            "hosting_mode",
            "is_shared",
            "max_mt5_instances",
            "supports_autonomous_execution",
            "reset_on_logout",
            "is_user_visible",
        ]


class VpsInstanceSerializer(serializers.ModelSerializer):
    provider_name = serializers.CharField(source="provider.name", read_only=True)
    plan_name = serializers.CharField(source="plan.name", read_only=True)

    class Meta:
        model = VpsInstance
        fields = [
            "id",
            "provider",
            "provider_name",
            "plan",
            "plan_name",
            "external_id",
            "hostname",
            "public_ip",
            "status",
            "is_dedicated",
            "current_mt5_count",
            "provisioned_at",
            "last_health_check_at",
        ]


class Mt5InstanceSerializer(serializers.ModelSerializer):
    vps_hostname = serializers.CharField(source="vps.hostname", read_only=True)
    vps_ip = serializers.CharField(source="vps.public_ip", read_only=True)
    owner_email = serializers.EmailField(source="owner.email", read_only=True)

    class Meta:
        model = Mt5Instance
        fields = [
            "id",
            "label",
            "broker_name",
            "account_login",
            "owner",
            "owner_email",
            "vps",
            "vps_hostname",
            "vps_ip",
            "status",
            "created_at",
            "updated_at",
        ]


class UserMt5InstanceMiniSerializer(serializers.ModelSerializer):
    class Meta:
        model = Mt5Instance
        fields = [
            "id",
            "label",
            "broker_name",
            "account_login",
            "status",
            "created_at",
        ]


class UserHostingSubscriptionMeSerializer(serializers.ModelSerializer):
    plan_name = serializers.CharField(source="plan.name", read_only=True)
    provider_name = serializers.CharField(source="plan.provider.name", read_only=True)
    vps_hostname = serializers.CharField(source="vps.hostname", read_only=True)
    vps_ip = serializers.CharField(source="vps.public_ip", read_only=True)
    mt5_instance_detail = UserMt5InstanceMiniSerializer(
        source="mt5_instance", read_only=True
    )

    class Meta:
        model = UserHostingSubscription
        fields = [
            "id",
            "plan_name",
            "provider_name",
            "billing_status",
            "vps_hostname",
            "vps_ip",
            "mt5_instance_detail",
            "created_at",
            "updated_at",
        ]


class HostingRequestSerializer(serializers.ModelSerializer):
    owner_email = serializers.EmailField(source="owner.email", read_only=True)

    class Meta:
        model = HostingRequest
        fields = [
            "id",
            "owner",
            "owner_email",
            "status",
            "note",
            "created_at",
            "updated_at",
        ]
        read_only_fields = [
            "id",
            "owner",
            "owner_email",
            "status",
            "created_at",
            "updated_at",
        ]
