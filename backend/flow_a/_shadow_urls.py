"""Minimal URLconf for the isolated Flow A shadow settings shim.

Flow A exposes no HTTP endpoints (Shadow Mode, no production surface). This empty
URLconf exists only so the isolated settings can boot for the demo command and
tests without importing the full production URL tree.
"""

urlpatterns: list = []
