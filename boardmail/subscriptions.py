"""Bounded provider progress for explicitly subscribed public threads.

The core supplies ``settings["subscriptions"]`` as root IDs selected locally.
Providers keep per-root progress under ``state["subscriptions"]`` only while a
root stays subscribed; ordinary notification and configured-thread progress is
never touched here.
"""
from .config import MailError, uuid

STATE_KEY = "subscriptions"


def selected(settings):
    """Sorted, deduplicated root UUIDs; an empty list keeps every current request."""
    roots = settings.get(STATE_KEY) or []
    try:
        if not isinstance(roots, list): raise ValueError("Invalid subscriptions")
        return sorted(dict.fromkeys(uuid(root) for root in roots))
    except (ValueError, TypeError, AttributeError):
        raise MailError("invalid_config") from None


def progress(state, roots):
    """Per-root progress pruned to current subscriptions, stored only when any exist."""
    entry = state.get(STATE_KEY)
    if not isinstance(entry, dict) or not isinstance(entry.get("roots"), dict):
        entry = {"next": None, "roots": {}}
    entry["roots"] = {root: value for root, value in entry["roots"].items()
                      if root in roots and isinstance(value, dict)}
    if roots:
        state[STATE_KEY] = entry
    else:
        state.pop(STATE_KEY, None)
    return entry


def rotation(entry, roots):
    """Roots in sorted order starting at the saved position; a removed root cannot block."""
    start = entry.get("next")
    ordered = sorted(roots)
    if not isinstance(start, str) or not ordered:
        return ordered
    index = next((i for i, root in enumerate(ordered) if root >= start), 0)
    return ordered[index:] + ordered[:index]


def advance(entry, roots, resume):
    """Remember where the next pass starts: the interrupted root, or the one after the last."""
    ordered = sorted(roots)
    if not ordered:
        entry["next"] = None
    elif resume in ordered:
        entry["next"] = resume
    else:
        entry["next"] = ordered[0]
