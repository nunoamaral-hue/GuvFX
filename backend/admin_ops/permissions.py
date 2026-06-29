"""
RBAC permission layer for the Admin Operations Console.

Roles are mapped to Django ``auth.Group`` names:
  - ``super_admin``   — full access to all admin APIs
  - ``finance_admin`` — reconciliation workflow, payment/execution read-only
  - ``ops_admin``     — execution/worker ops, reconciliation ack-only

Permission classes below check group membership on ``request.user``.
``is_superuser`` is accepted as an implicit ``super_admin``.

This module is intentionally narrow — it covers Packet 3 surfaces only
and does NOT redesign the existing auth subsystem.
"""

from __future__ import annotations

from rest_framework.permissions import BasePermission

# ---------------------------------------------------------------------------
# Role constants
# ---------------------------------------------------------------------------

ROLE_SUPER_ADMIN = "super_admin"
ROLE_FINANCE_ADMIN = "finance_admin"
ROLE_OPS_ADMIN = "ops_admin"

ALL_ADMIN_ROLES = frozenset({ROLE_SUPER_ADMIN, ROLE_FINANCE_ADMIN, ROLE_OPS_ADMIN})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _user_roles(user) -> set[str]:
    """Return the set of admin role names the user belongs to."""
    if not user or not user.is_authenticated:
        return set()
    # is_superuser is treated as implicit super_admin
    roles = set(user.groups.values_list("name", flat=True)) & ALL_ADMIN_ROLES
    if user.is_superuser:
        roles.add(ROLE_SUPER_ADMIN)
    return roles


def user_has_any_role(user, *roles: str) -> bool:
    """Return True if the user holds at least one of the given roles."""
    return bool(_user_roles(user) & set(roles))


def user_has_role(user, role: str) -> bool:
    return user_has_any_role(user, role)


# ---------------------------------------------------------------------------
# DRF Permission classes
# ---------------------------------------------------------------------------

class IsAdminRole(BasePermission):
    """
    Allows access if the user holds ANY admin role (super_admin, finance_admin,
    or ops_admin).  Use as a base gate before applying finer checks in views.
    """
    message = "Admin role required."

    def has_permission(self, request, view):
        return bool(
            request.user
            and request.user.is_authenticated
            and _user_roles(request.user)
        )


class IsSuperAdmin(BasePermission):
    """Only super_admin."""
    message = "super_admin role required."

    def has_permission(self, request, view):
        return user_has_role(request.user, ROLE_SUPER_ADMIN)


class IsSuperOrFinanceAdmin(BasePermission):
    """super_admin OR finance_admin."""
    message = "super_admin or finance_admin role required."

    def has_permission(self, request, view):
        return user_has_any_role(
            request.user, ROLE_SUPER_ADMIN, ROLE_FINANCE_ADMIN,
        )


class IsSuperOrOpsAdmin(BasePermission):
    """super_admin OR ops_admin."""
    message = "super_admin or ops_admin role required."

    def has_permission(self, request, view):
        return user_has_any_role(
            request.user, ROLE_SUPER_ADMIN, ROLE_OPS_ADMIN,
        )


class IsSuperOrFinanceOrOpsAdmin(BasePermission):
    """Any of super_admin, finance_admin, ops_admin."""
    message = "Admin role required."

    def has_permission(self, request, view):
        return user_has_any_role(
            request.user,
            ROLE_SUPER_ADMIN,
            ROLE_FINANCE_ADMIN,
            ROLE_OPS_ADMIN,
        )
