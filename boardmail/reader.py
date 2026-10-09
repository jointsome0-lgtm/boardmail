"""Bounded local reading views. Only proven thread activity loses its body."""
import json

from . import boards
from .config import MailError
from .errors import route

DEFAULTS = {"scope": "addressed", "context": "brief"}
CHOICES = {"scope": ("addressed", "all"), "context": ("brief", "none")}
# The arrivals on a page of check, list or wait that names no limit. A replay names none, so it has this size.
PAGE_SIZE = 20
# What a message in a result has only where it holds something. A field that is absent is none, not unknown.
SPARSE = ("parent_id", "provider_seq", "read_at", "needs_reply", "replied_at", "reply_ref", "discovery", "tags")
SHOWN_BECAUSE = {
    "direct": "direct_reply_to_your_message",
    "mention": "mention_detected_may_be_quoted",
    "direct+mention": "direct_reply_and_mention_detected",
    "thread": "thread_activity_without_confirmed_direct_reply_or_mention",
    None: "recipient_unconfirmed_shown_by_default",
}


def validate_options(scope=None, context=None):
    for key, value in (("scope", scope), ("context", context)):
        if value is not None and value not in CHOICES[key]:
            raise MailError("invalid_arguments", argument=key)


def written(message):
    """A message as a result has it: without the fields of SPARSE that hold nothing, which is null, false or an
    empty list. Inside the package a message has them all."""
    return {key: value for key, value in message.items()
            if key not in SPARSE or not (value is None or value is False or value == [])}


def excerpt(item, message, budget=600, **first):
    """What a brief says of another message or original: who wrote it and its text, cut to the budget. Its thread
    and its title only where they are not those of the message that the brief belongs to, and truncated only
    where it was cut."""
    said = {"id": item.get("id"), **first, "author": item.get("author")}
    if item.get("thread_id") != message["thread_id"]:
        said["thread_id"] = item.get("thread_id")
    if item["title"] != message["title"]:
        said["title"] = item["title"][:160]
    cut = len(item["body"]) > budget or "title" in said and len(item["title"]) > 160
    return said | {"body": item["body"][:budget]} | ({"truncated": True} if item.get("truncated") or cut else {})


def brief(db, item):
    source, root_id, parent_id = item["source"], item["thread_id"], item["parent_id"]
    row = db.execute("SELECT adapter FROM adapter_state WHERE source=?", (source,)).fetchone()
    board = boards.declared(source if row is None else row[0])

    def resolve(mid):
        row = db.execute("SELECT * FROM messages WHERE source=? AND id=?", (source, mid)).fetchone()
        if row is not None:
            if row["thread_id"] != root_id:
                return {"id": mid, "status": "unavailable", "reason": "thread_mismatch"}
            return excerpt(dict(row), item, status="stored")
        row = db.execute("SELECT value FROM originals WHERE source=? AND id=?", (source, mid)).fetchone()
        if row is not None:
            cached = json.loads(row["value"])
            if cached["thread_id"] == root_id:
                return excerpt(cached, item, status="cached")
            return {"id": mid, "status": "unavailable", "reason": "thread_mismatch"}
        return {"id": mid, "status": "not_available_locally"}

    root = {"id": root_id, "status": "current_message"} if root_id == item["id"] else resolve(root_id)
    if item["id"] == root_id:
        parent = {"id": None, "status": "none"}
    elif parent_id is None:
        parent = {"id": None, "status": "unknown"}
    elif parent_id == root_id:
        parent = {"id": parent_id, "status": "same_as_root"}
    else:
        parent = resolve(parent_id)

    # Exact published-parent links only, scoped to this source. Marks alone
    # neither establish a relationship nor prove that a question is closed.
    exchange = {"status": "unknown", "messages": []}
    if parent_id is not None and parent["status"] != "unavailable":
        try:
            ref = board.reference(root_id, parent_id) if board.reference else None
        except (ValueError, TypeError, AttributeError):
            ref = None
        if ref is not None:
            rows = db.execute("SELECT * FROM messages WHERE source=? AND reply_ref=? AND id<>? "
                              "ORDER BY arrival_seq LIMIT 3", (source, ref, item["id"])).fetchall()
            exchange = {"status": "linked" if rows else "unmatched",
                        "messages": [excerpt(dict(row), item, 200) for row in rows[:2]], "more": len(rows) > 2}
    return {"root": root, "parent": parent, "previous_exchange": exchange}


def present(db, result, *, scope, context):
    messages, activity = [], {}
    for item in result["messages"]:
        if scope == "addressed" and item["addressing"] == "thread":
            key = (item["source"], item["thread_id"])
            summary = activity.setdefault(key, {"source": key[0], "thread_id": key[1], "tags": item['tags'], "count": 0,
                "unread": 0, "first_seq": item["arrival_seq"], "last_seq": item["arrival_seq"],
                "reason": "thread_activity_without_confirmed_direct_reply_or_mention"})
            summary["count"] += 1
            summary["unread"] += int(item["read_at"] is None)
            summary["last_seq"] = item["arrival_seq"]
        else:
            item["shown_because"] = SHOWN_BECAUSE[item["addressing"]]
            if context == "brief":
                item["brief"] = brief(db, item)
            messages.append(written(item))
    for summary in activity.values():
        # An inclusive upper bound prevents newer arrivals leaking into replay.
        # Omit unread: explicit marks may have changed since the summary.
        within = {"source": summary["source"], "thread": summary["thread_id"],
                  "after": summary["first_seq"] - 1, "through": summary["last_seq"]}
        summary["replay"] = route("list", **within, scope="all", context="none")
        summary["expand"] = route("expand", **within, limit=20)
    if not result["checkpoint_safe"]:
        action = "process_filtered_page_keep_delivery_checkpoint"
    else:
        action = "process_messages_and_thread_activity_then_save_next_after" if result["messages"] else "collect_or_wait"
    return {**result, "messages": messages, "thread_activity": list(activity.values()),
            "reading": {"scope": scope, "context": context},
            "scanned": len(result["messages"]),
            "next_action": action}
