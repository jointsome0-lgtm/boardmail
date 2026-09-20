"""Bounded local reading views. Only proven thread activity loses its body."""
import json

from .config import MailError

DEFAULTS = {"scope": "addressed", "context": "brief"}
CHOICES = {"scope": ("addressed", "all"), "context": ("brief", "none")}
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
            raise MailError("invalid_arguments")


def excerpt(item, budget=600):
    return {key: item.get(key) for key in ("id", "thread_id", "author", "url")} | {
        "title": item["title"][:160], "body": item["body"][:budget],
        "truncated": bool(item.get("truncated") or len(item["body"]) > budget or len(item["title"]) > 160)}


def brief(db, item):
    source, root_id, parent_id = item["source"], item["thread_id"], item["parent_id"]
    adapter = source
    if db.execute("PRAGMA user_version").fetchone()[0] >= 2:
        row = db.execute("SELECT adapter FROM adapter_state WHERE source=?", (source,)).fetchone()
        if row is not None:
            adapter = row[0]
    if adapter == "fourclaw":
        # Its legacy parent_id is synthesized thread membership, not a reply target.
        parent_id = None

    def resolve(mid):
        row = db.execute("SELECT * FROM messages WHERE source=? AND id=?", (source, mid)).fetchone()
        if row is not None:
            if row["thread_id"] != root_id:
                return {"id": mid, "status": "unavailable", "reason": "thread_mismatch"}
            return {"status": "stored", **excerpt(dict(row))}
        if db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='originals'").fetchone():
            row = db.execute("SELECT value,fetched_at FROM originals WHERE source=? AND id=?", (source, mid)).fetchone()
            if row is not None:
                cached = json.loads(row["value"])
                if cached["thread_id"] == root_id:
                    return {"status": "cached", "fetched_at": row["fetched_at"], **excerpt(cached)}
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
        from .providers import parent_reference
        try:
            ref = parent_reference(adapter, root_id, parent_id)
        except (ValueError, TypeError, AttributeError):
            ref = None
        if ref is not None:
            rows = db.execute("SELECT * FROM messages WHERE source=? AND reply_ref=? AND id<>? "
                              "ORDER BY arrival_seq LIMIT 3", (source, ref, item["id"])).fetchall()
            exchange = {"status": "linked" if rows else "unmatched", "reply_ref": ref,
                        "messages": [excerpt(dict(row), 200) for row in rows[:2]], "more": len(rows) > 2}
    return {"root": root, "parent": parent, "previous_exchange": exchange,
            "expand": {"command": "context", "arguments": {"source": source, "id": item["id"]}}}


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
            messages.append(item)
    for summary in activity.values():
        # An inclusive upper bound prevents newer arrivals leaking into replay.
        # Omit unread: explicit marks may have changed since the summary.
        summary["replay"] = {"command": "list", "arguments": {
            "source": summary["source"], "thread": summary["thread_id"],
            "after": summary["first_seq"] - 1, "through": summary["last_seq"],
            "limit": 500, "scope": "all", "context": "none"}}
        summary["expand"] = {"command": "expand", "arguments": {
            "source": summary["source"], "thread": summary["thread_id"],
            "after": summary["first_seq"] - 1, "through": summary["last_seq"], "limit": 20}}
    if not result["checkpoint_safe"]:
        action = "process_filtered_page_keep_delivery_checkpoint"
    else:
        action = "process_messages_and_thread_activity_then_save_next_after" if result["messages"] else "collect_or_wait"
    return {**result, "messages": messages, "thread_activity": list(activity.values()),
            "reading": {"scope": scope, "context": context},
            "scanned": len(result["messages"]),
            "next_action": action}
