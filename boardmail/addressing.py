"""Addressing evidence recorded by collectors: who a public original was written to.

Values: ``direct`` (a reply to something this account authored), ``mention``
(an explicit native or textual @mention), ``direct+mention``, ``thread`` (activity
inside a thread without a confirmed direct reply or mention) or ``None`` (unknown).
Only actual provider evidence seen during collection sets a value; the legacy
``kind`` is never a source. Unknown stays visible to readers.

The same module keeps already fetched public originals for the core's bounded
cache: roots, parents and our own authored posts that the inbox itself discards.
"""
import re

VALUES = ("direct", "mention", "direct+mention", "thread")
PROFILE_KEYS = ("username", "name", "handle")
MAX_ALIAS = 100
MAX_ORIGINALS = 256
ORIGINAL_FIELDS = ("id", "thread_id", "parent_id", "author", "title", "body", "url", "created_at")


def resolve(*, direct=False, mention=False, thread=False):
    """Combine evidence; a direct target outranks thread membership."""
    if direct:
        return "direct+mention" if mention else "direct"
    if mention:
        return "mention"
    return "thread" if thread else None


def aliases(profile, configured=()):
    """Names the board itself verified for this account plus configured ones.

    Leading ``@`` is dropped so every alias matches as an explicit ``@alias``.
    """
    found = []
    if isinstance(profile, dict):
        for key in PROFILE_KEYS:
            value = profile.get(key)
            if isinstance(value, str):
                found.append(value)
    for alias in configured or ():
        if isinstance(alias, str):
            found.append(alias)
    result = {}
    for alias in found:
        alias = alias.strip().lstrip("@").strip()
        if alias and len(alias) <= MAX_ALIAS and alias.casefold() not in result:
            result[alias.casefold()] = alias
    return list(result.values())


def mention_pattern(names):
    """Explicit ``@alias`` with name boundaries; partial handles never match."""
    names = [n for n in names if n]
    if not names:
        return None
    return re.compile(r"(?<![\w@])@(?:" + "|".join(re.escape(n) for n in names) + r")(?![\w-])", re.IGNORECASE)


def mentions(pattern, *texts):
    return pattern is not None and any(isinstance(t, str) and pattern.search(t) for t in texts)


def cache_original(batch, item):
    """Retain one fully fetched public original; bounded and deduplicated per batch.

    Callers pass complete originals only: never notification previews,
    truncated summaries, deleted or hidden material.
    """
    items = batch.originals
    if len(items) >= MAX_ORIGINALS or any(o["id"] == item["id"] for o in items):
        return False
    items.append({key: item.get(key) for key in ORIGINAL_FIELDS})
    return True
