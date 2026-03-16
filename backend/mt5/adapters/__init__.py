"""
Packet A — Session Adapter Boundary.

Provides a replaceable adapter interface for terminal session
lifecycle operations (launch, resume, terminate, status).

The concrete Guacamole + VNC adapter is behind this interface.
Domain/service layers depend on the interface only — never on
Guacamole internals.
"""
