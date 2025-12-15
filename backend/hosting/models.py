from django.conf import settings
from django.db import models

User = settings.AUTH_USER_MODEL

HOSTING_MODE_CHOICES = [
  ("SESSION_EPHEMERAL", "Ephemeral per-session instance"),
  ("MANAGED_MT5_DEDICATED", "Managed dedicated MT5"),
  ("MANAGED_MT5_SHARED", "Managed shared MT5"),
]

PLAN_CODE_CHOICES = [
  ("FREE_SESSION_MT5", "Free MT5 session (ephemeral)"),
  ("STANDARD_DEDICATED_2", "Standard – 2 dedicated MT5 instances"),
  ("MANAGED_SHARED_10", "Managed shared – up to 10 MT5 instances"),
]


class HostingProvider(models.Model):
  """
  Represents a hosting provider (e.g. OVH).
  For v1 we only use OVH + manual provisioning, but this gives us room to add more later.
  """

  name = models.CharField(max_length=100, unique=True)
  api_type = models.CharField(
    max_length=50,
    choices=(
      ("OVH_MANUAL", "OVH (manual)"),
      ("OVH_API", "OVH (API)"),
    ),
    default="OVH_MANUAL",
  )
  api_base_url = models.URLField(blank=True)
  is_active = models.BooleanField(default=True)

  class Meta:
    verbose_name = "Hosting provider"
    verbose_name_plural = "Hosting providers"

  def __str__(self) -> str:
    return self.name


class VpsPlan(models.Model):
  """
  A logical plan users can choose (Scalper, Pro, etc.).
  Maps to a provider-specific plan slug but keeps GuvFX naming / pricing separate.
  """

  provider = models.ForeignKey(HostingProvider, on_delete=models.PROTECT, related_name="plans")
  name = models.CharField(max_length=100)
  description = models.TextField(blank=True)

  cpu_cores = models.PositiveSmallIntegerField()
  memory_mb = models.PositiveIntegerField()
  disk_gb = models.PositiveIntegerField()

  monthly_price_usd = models.DecimalField(max_digits=10, decimal_places=2)

  provider_plan_slug = models.CharField(
    max_length=100,
    help_text="Identifier used by the provider API for this size/plan.",
  )

  code = models.CharField(
    max_length=50,
    unique=True,
    choices=PLAN_CODE_CHOICES,
    null=True,
    blank=True,
    help_text="Internal code for this plan (used in logic and UI).",
  )

  hosting_mode = models.CharField(
    max_length=32,
    choices=HOSTING_MODE_CHOICES,
    default=HOSTING_MODE_CHOICES[0][0],
    help_text="Execution/hosting mode for MT5 instances created under this plan.",
  )

  is_shared = models.BooleanField(
    default=False,
    help_text="If true, multiple users can share a single VPS (pool model). If false, one VPS per subscription.",
  )
  max_mt5_instances = models.PositiveSmallIntegerField(
    default=1,
    help_text="Maximum number of MT5 terminals per VPS for this plan.",
  )

  supports_autonomous_execution = models.BooleanField(
    default=False,
    help_text="If true, MT5 instances may run 24/7 even when the user is logged out.",
  )

  reset_on_logout = models.BooleanField(
    default=False,
    help_text="If true, MT5 instances are destroyed or hard-reset when the user logs out.",
  )

  is_user_visible = models.BooleanField(
    default=True,
    help_text="Whether this plan should be shown in the end-user UI.",
  )

  class Meta:
    verbose_name = "VPS plan"
    verbose_name_plural = "VPS plans"

  def __str__(self) -> str:
    return f"{self.name} ({self.code})"


class VpsInstance(models.Model):
  """
  A concrete VPS at the provider (e.g. your existing OVH VPS).
  Can be dedicated to one user or part of a shared pool.
  """

  provider = models.ForeignKey(HostingProvider, on_delete=models.PROTECT, related_name="instances")
  plan = models.ForeignKey(VpsPlan, on_delete=models.PROTECT, related_name="instances")

  external_id = models.CharField(
    max_length=100,
    blank=True,
    help_text="Provider's identifier for this VPS (if known).",
  )
  hostname = models.CharField(max_length=255, blank=True)
  public_ip = models.GenericIPAddressField(blank=True, null=True)

  guac_connection_id = models.CharField(
    max_length=128,
    null=True,
    blank=True,
    help_text="Guacamole connection/client identifier for this VPS (for opening the remote console).",
  )

  STATUS_CHOICES = (
    ("ALLOCATING", "Allocating"),
    ("ACTIVE", "Active"),
    ("ERROR", "Error"),
    ("DECOMMISSIONED", "Decommissioned"),
  )
  status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="ALLOCATING")

  is_dedicated = models.BooleanField(
    default=False,
    help_text="True if this VPS is dedicated to a single user/subscription.",
  )

  current_mt5_count = models.PositiveSmallIntegerField(default=0)

  provisioned_at = models.DateTimeField(blank=True, null=True)
  last_health_check_at = models.DateTimeField(blank=True, null=True)

  class Meta:
    verbose_name = "VPS instance"
    verbose_name_plural = "VPS instances"

  @property
  def display_name(self) -> str:
    if getattr(self, "hostname", None):
      return self.hostname  # type: ignore[attr-defined]
    return f"VPS #{self.pk}"

  def __str__(self) -> str:
    label = self.display_name or self.public_ip or f"VPS #{self.pk}"
    return f"{label} ({self.plan.name})"


class Mt5Instance(models.Model):
  """
  An MT5 terminal installation associated with a user and a VPS.
  The MT5 account credentials stay in your existing secure storage; this
  just describes the terminal itself.
  """

  vps = models.ForeignKey(VpsInstance, on_delete=models.PROTECT, related_name="mt5_instances")
  owner = models.ForeignKey(User, on_delete=models.CASCADE, related_name="mt5_instances")

  label = models.CharField(
    max_length=100,
    help_text="Friendly name for this MT5 instance (e.g. XM Pro 1).",
  )
  broker_name = models.CharField(max_length=100)
  account_login = models.CharField(max_length=64)

  install_path = models.CharField(
    max_length=255,
    blank=True,
    help_text="Filesystem path to MT5 installation on the VPS, if needed.",
  )

  STATUS_CHOICES = (
    ("PENDING_INSTALL", "Pending install"),
    ("READY", "Ready"),
    ("ERROR", "Error"),
  )
  status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING_INSTALL")

  created_at = models.DateTimeField(auto_now_add=True)
  updated_at = models.DateTimeField(auto_now=True)

  class Meta:
    verbose_name = "MT5 instance"
    verbose_name_plural = "MT5 instances"

  def __str__(self) -> str:
    return f"{self.label} ({self.broker_name} {self.account_login})"


class UserHostingSubscription(models.Model):
  """
  Represents that a user has a hosting subscription with a particular plan.
  For shared plans, vps may be a pool node; for dedicated plans, it is one-to-one.
  """

  user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hosting_subscriptions")
  plan = models.ForeignKey(VpsPlan, on_delete=models.PROTECT, related_name="subscriptions")

  vps = models.ForeignKey(
    VpsInstance,
    on_delete=models.PROTECT,
    related_name="subscriptions",
    blank=True,
    null=True,
    help_text="Assigned VPS for this subscription, if known.",
  )
  mt5_instance = models.ForeignKey(
    Mt5Instance,
    on_delete=models.PROTECT,
    related_name="subscriptions",
    blank=True,
    null=True,
    help_text="Primary MT5 terminal associated with this subscription, if applicable.",
  )

  STATUS_TRIAL = "TRIAL"
  STATUS_ACTIVE = "ACTIVE"
  STATUS_PAST_DUE = "PAST_DUE"
  STATUS_CANCELLED = "CANCELLED"

  BILLING_STATUS_CHOICES = (
    (STATUS_TRIAL, "Trial"),
    (STATUS_ACTIVE, "Active"),
    (STATUS_PAST_DUE, "Past due"),
    (STATUS_CANCELLED, "Cancelled"),
  )
  billing_status = models.CharField(
    max_length=20,
    choices=BILLING_STATUS_CHOICES,
    default=STATUS_ACTIVE,
  )

  created_at = models.DateTimeField(auto_now_add=True)
  updated_at = models.DateTimeField(auto_now=True)

  class Meta:
    verbose_name = "User hosting subscription"
    verbose_name_plural = "User hosting subscriptions"

  def __str__(self) -> str:
    return f"{self.user} – {self.plan.name} ({self.billing_status})"


class HostingRequest(models.Model):
  """
  A simple record that a user has requested hosted MT5.
  """

  class Status(models.TextChoices):
    PENDING = "PENDING", "Pending"
    IN_REVIEW = "IN_REVIEW", "In review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    PROVISIONED = "PROVISIONED", "Provisioned"
    COMPLETED = "COMPLETED", "Completed"

  owner = models.ForeignKey(
    settings.AUTH_USER_MODEL,
    on_delete=models.CASCADE,
    related_name="hosting_requests",
  )
  status = models.CharField(
    max_length=32,
    choices=Status.choices,
    default=Status.PENDING,
  )
  note = models.TextField(
    blank=True,
    help_text="Optional message from the user describing their needs.",
  )
  created_at = models.DateTimeField(auto_now_add=True)
  updated_at = models.DateTimeField(auto_now=True)

  class Meta:
    ordering = ["-created_at"]

  def __str__(self) -> str:
    return f"HostingRequest #{self.pk} ({self.owner} – {self.status})"
