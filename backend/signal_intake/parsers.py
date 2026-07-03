"""
SIGNAL-ACQUISITION-MVP — parser registry.

Maps a ``ParserProfile.slug`` to a parser callable ``(text, message_id) ->
ParsedSignal``. New provider *formats* register a new profile here rather than
editing a shared parser. MVP ships ``wayond_v1`` wrapping the existing, deployed
Wayond parser (``intelligence.telegram_source.parse_message``).
"""

from __future__ import annotations

from intelligence.telegram_source import parse_message as _wayond_parse

# slug -> callable(text, message_id) -> ParsedSignal (never raises)
_REGISTRY = {
    "wayond_v1": _wayond_parse,
}


class UnknownParserProfile(Exception):
    """Raised when a provider references a parser profile slug with no callable."""


def get_parser(slug: str):
    """Return the parser callable for ``slug`` or raise ``UnknownParserProfile``.

    The dispatcher treats this raise as a fail-closed quarantine (never allows an
    unparsed message through)."""
    try:
        return _REGISTRY[slug]
    except KeyError:
        raise UnknownParserProfile(f"no parser registered for profile {slug!r}")


def registered_profiles() -> tuple:
    return tuple(_REGISTRY)
