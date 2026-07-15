"""Human-friendly PUBLIC display labels for internal source/provider slugs.

Presentation-only. The internal slugs (``ti_signals``, ``wayond``) are the source of truth for
routing, config and execution and are NEVER changed here. This is the ONE reusable mapping so
renderers don't format raw slugs independently (e.g. ``TI_SIGNALS`` on a stakeholder card).
"""
from __future__ import annotations

# Exact public labels for known provider/source slugs. Keys are lower-cased slugs.
_SOURCE_LABELS = {
    "ti_signals": "TI Signals",
    "ti_signals_telegram": "TI Signals",
    "wayond": "Wayond",
    "wayond_telegram": "Wayond",
}


def source_display_label(slug: str) -> str:
    """Public label for a provider/source slug, e.g. ``ti_signals`` -> ``TI Signals``.

    Unknown slugs degrade safely to a title-cased, underscore-free form (``foo_bar`` -> ``Foo Bar``);
    an empty/None slug falls back to ``GuvFX`` so a card never shows a blank source.
    """
    s = (slug or "").strip()
    if not s:
        return "GuvFX"
    return _SOURCE_LABELS.get(s.lower(), s.replace("_", " ").title())
