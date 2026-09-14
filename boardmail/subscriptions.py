"""Bounded provider progress for explicitly subscribed public threads.

The core supplies ``settings["subscriptions"]`` as root IDs selected locally.
Providers keep per-root progress under ``state["subscriptions"]`` only while a
root stays subscribed; ordinary notification and configured-thread progress is
never touched here. A root that consumes its pass keeps its own position and
the next pass starts at the following root, so one slow thread cannot starve
the others.
"""
from .config import MailError, uuid

STATE_KEY = "subscriptions"
MAX_OWNERS = 400  # Retained ownership of fetched comments per root, oldest dropped first.


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


def following(roots, root):
    """The root after ``root`` in sorted order, wrapping; the first when it is unknown."""
    ordered = sorted(roots)
    if not ordered:
        return None
    if root not in ordered:
        return ordered[0]
    return ordered[(ordered.index(root) + 1) % len(ordered)]


def advance(entry, roots, resume):
    """Remember where the next pass starts: the given root, or the first after a full pass."""
    ordered = sorted(roots)
    if not ordered:
        entry["next"] = None
    elif resume in ordered:
        entry["next"] = resume
    else:
        entry["next"] = ordered[0]


def restart(roots, root, consumed):
    """Where the next pass starts after ``root`` was cut short: the root itself when it
    had not yet read anything, otherwise the following one, so it waits one turn."""
    return following(roots, root) if consumed else root


def owners(progress):
    """The saved ownership map of a root, verified as ``id -> True/False/None``."""
    saved = progress.get("owners")
    if not isinstance(saved, dict):
        return {}
    kept = {}
    for key, value in saved.items():
        try:
            if value in (True, False, None) and uuid(key) == key:
                kept[key] = value
        except (ValueError, TypeError, AttributeError):
            continue
    return kept


def remember(owned, key, value):
    """Record fetched ownership; the oldest entry leaves when the map is full."""
    if key in owned:
        del owned[key]
    owned[key] = value
    while len(owned) > MAX_OWNERS:
        del owned[next(iter(owned))]


def store_owners(progress, owned):
    if owned:
        progress["owners"] = owned
    else:
        progress.pop("owners", None)
