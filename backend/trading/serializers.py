from django.core.exceptions import ObjectDoesNotExist
from rest_framework import serializers
from .models import TradingAccount, BrokerServer, Trade, BrokerAccountValidationAttempt
from .crypto import encrypt_password
from .classification import classification_error
from core.audit import log_customer_credential_event


class TradingAccountSerializer(serializers.ModelSerializer):
    broker_server = serializers.PrimaryKeyRelatedField(
        queryset=BrokerServer.objects.filter(is_active=True),
        required=False,
        allow_null=True,
    )

    broker_display_name = serializers.CharField(source="broker_server.broker_display_name", read_only=True)
    server_name = serializers.CharField(source="broker_server.server_name", read_only=True)

    # Accept plaintext password in request, store encrypted in password_enc
    password = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    mt5_instance = serializers.PrimaryKeyRelatedField(read_only=True)
    # IPR Area B (C6): truthful dedicated-runtime signal. For a beta account ``mt5_instance`` is always
    # ``null`` by design (ADR-0021), so the frontend must gate on runtime readiness — not on the legacy
    # instance FK — to avoid telling a customer "no terminal" while their AccountRuntime is RUNNING.
    # The runtime lookup is query-free (reverse OneToOne prefetched via ``select_related("runtime")``);
    # runtime_ready adds ONE verification-report query PER BETA+RUNNING row only (production/non-RUNNING
    # rows short-circuit before it), which is negligible at beta-list scale.
    runtime_ready = serializers.SerializerMethodField()
    runtime_state = serializers.SerializerMethodField()

    class Meta:
        model = TradingAccount
        fields = [
            "id",
            "name",
            "mt5_instance",
            "runtime_ready",
            "runtime_state",
            "broker_server",
            "broker_display_name",
            "server_name",
            "broker_name",
            "account_number",
            "is_demo",
            "is_active",
            "created_at",
            "updated_at",
            "password",
        ]
        read_only_fields = ["id", "created_at", "updated_at"]

    def _runtime(self, obj):
        """The account's owned AccountRuntime (reverse OneToOne, prefetchable), or None. Never raises."""
        try:
            return obj.runtime
        except ObjectDoesNotExist:
            return None

    def get_runtime_ready(self, obj):
        from terminal_provisioning.beta_activation import runtime_ready
        rt = self._runtime(obj)
        return bool(rt is not None and runtime_ready(rt))

    def get_runtime_state(self, obj):
        rt = self._runtime(obj)
        return rt.state if rt is not None else None

    def validate(self, attrs):
        broker_server = attrs.get("broker_server") or getattr(self.instance, "broker_server", None)
        broker_name = (attrs.get("broker_name") or getattr(self.instance, "broker_name", "") or "").strip()
        if not broker_server and not broker_name:
            raise serializers.ValidationError("Provide either broker_server or broker_name.")

        # T7 (Phase 3 / P3-E): demo/live classification consistency, via the shared config-level check
        # (also enforced at the add-with-mt5-login create endpoint). Scoped to requests that set the
        # classification fields, so it never retroactively blocks an unrelated edit on a legacy
        # inconsistent row. Broker-TRUTH verification (account_info().trade_mode) stays at execution.
        if broker_server is not None and ("is_demo" in attrs or "broker_server" in attrs):
            is_demo = attrs.get("is_demo", getattr(self.instance, "is_demo", False))
            err = classification_error(is_demo, broker_server)
            if err:
                raise serializers.ValidationError({"is_demo": err})

        # Beta UX Correction part C — a hosted (Provider-B) account's broker identity is WRITE-ONCE. Once bound,
        # account_number / broker_server cannot be changed through the generic account API (the authoritative
        # model-layer guard in TradingAccount.save enforces this too; rejecting here returns a clean 400 rather
        # than a 500 and keeps the identity solely under the dedicated bind_broker_identity seam). This closes
        # the re-pin path that would otherwise let an owner silently re-point a confirmed hosted workspace at a
        # different broker account and defeat account-switch detection. Legacy / Provider-A accounts unaffected.
        inst = self.instance
        if inst is not None:
            from execution.readiness import PERSISTENT_WORKSPACE
            cur_login = str(getattr(inst, "account_number", "") or "").strip()
            if str(getattr(inst, "readiness_provider", "")) == PERSISTENT_WORKSPACE:
                # Hosted (Provider-B) identity AND classification are managed SOLELY by the dedicated
                # bind_broker_identity seam (demo-only + pre-connected + audited) — INCLUDING the deferred
                # pre-bind window (cur_login == ''). The generic account API must never FIRST-BIND or change
                # them, or an owner could self-authorise an arbitrary/live identity here, skipping the seam's
                # BIND_LIVE_FORBIDDEN / BIND_WRONG_STATE checks and the identity-bound audit + telemetry. The
                # bind seam writes the model directly (not via this serializer), so it is unaffected.
                if "account_number" in attrs and str(attrs.get("account_number") or "").strip() != cur_login:
                    raise serializers.ValidationError(
                        {"account_number": "Hosted broker identity is managed via the workspace bind step and cannot be set here."})
                if "broker_server" in attrs and attrs.get("broker_server") != inst.broker_server:
                    raise serializers.ValidationError(
                        {"broker_server": "Hosted broker identity is managed via the workspace bind step and cannot be set here."})
                if "is_demo" in attrs and bool(attrs.get("is_demo")) != bool(getattr(inst, "is_demo", False)):
                    raise serializers.ValidationError(
                        {"is_demo": "Hosted account classification is managed via the workspace bind step and cannot be changed here."})
        return attrs

    def create(self, validated_data):
        raw_password = validated_data.pop("password", "") or ""
        legacy_pw = validated_data.pop("broker_password", "") if "broker_password" in validated_data else ""
        raw_password = raw_password or legacy_pw

        if raw_password:
            validated_data["password_enc"] = encrypt_password(raw_password)
            validated_data["broker_password"] = ""

        instance = super().create(validated_data)
        if raw_password:
            # Customer-credential audit (Phase 3): intake of a broker password. Redacted, no secret.
            log_customer_credential_event(
                "CREATED", account=instance, request=self.context.get("request"), purpose="intake")
        return instance

    def update(self, instance, validated_data):
        raw_password = validated_data.pop("password", "") or ""
        legacy_pw = validated_data.pop("broker_password", "") if "broker_password" in validated_data else ""
        raw_password = raw_password or legacy_pw

        if raw_password:
            instance.password_enc = encrypt_password(raw_password)
            instance.broker_password = ""

        instance = super().update(instance, validated_data)
        if raw_password:
            # A new password supplied for an existing account is a credential ROTATION.
            log_customer_credential_event(
                "ROTATED", account=instance, request=self.context.get("request"), purpose="intake-update")
        return instance


class BrokerValidationAttemptSerializer(serializers.ModelSerializer):
    """WP1A (ADR-0028) — secret-safe projection of a broker-login validation attempt. Read-only; the
    field set is the ADR-0027 allow-list only (no password / ciphertext / envelope / host path).
    WS-P3: ``correlation_id`` is NOT exposed on this customer-facing serializer — it is an operator diagnostic
    identifier of no customer use, and shipping it in the customer's JSON contradicts the customer-safety
    guarantee. The staff validation-timeline endpoint reads it from the model directly (staff-gated)."""

    class Meta:
        model = BrokerAccountValidationAttempt
        fields = [
            "id", "trigger", "status", "reason_code", "retryable", "is_demo",
            "server", "login_masked", "created_at",
        ]
        read_only_fields = fields


class TradeSerializer(serializers.ModelSerializer):
    class Meta:
        model = Trade
        fields = "__all__"
        read_only_fields = "__all__"
