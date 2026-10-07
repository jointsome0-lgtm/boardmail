"""Moltbook: retained notifications and subscribed threads, with anonymous public originals out of comment trees."""
from functools import partial
from urllib.error import HTTPError

from . import adapter_common, adapter_notifications, transport
from .adapter_common import FAILURES, MAX_PAGES, error_code, text
from .adapter_notifications import descendants, notification_message, page
from .adapters import Batch, public_comment
from .errors import MailError, uuid

NAME = "moltbook"
HOST = "https://www.moltbook.com"
transport.BOARDS[NAME] = transport.Board(protocol=None, **adapter_common.SHARED)


class Client(adapter_notifications.Client):
    host = HOST
    prefix = "/api/v1"
    profile = "/agents/me"
    kinds = {"post_comment": "reply_to_post", "comment_reply": "reply_to_comment", "mention": "mention"}
    event = ("type", "relatedPostId", "relatedCommentId")
    reply, activity = "comment_reply", "post_comment"
    body, author = "content", "name"
    pages = "/post/"
    by_thread = True

    @staticmethod
    def account(profile):
        return profile["agent"]

    def post(self, raw):
        return raw["post"]


def moltbook_lookup(client, mid, root=None, *, originals=None):
    """Find an anonymous original in its paginated comment tree.

    A stored comment supplies its thread ID. Without that relationship only a
    root can be fetched; a missing post endpoint cannot establish comment absence.
    """
    originals = {} if originals is None else originals
    thread_loaded = False
    try:
        thread = root or mid
        if (thread, thread) not in originals:
            post = client.get("/posts/" + thread)["post"]
            if uuid(post["id"]) != thread: raise ValueError("Unexpected thread")
            originals[thread, thread] = post
        post = originals[thread, thread]
        thread_loaded = True
        if post.get("is_deleted"):
            return ("deleted", None, None) if mid == thread else ("unavailable", "thread_deleted", None)
        if post.get("is_spam"): return "unavailable", "hidden_by_provider", None
        title = text(post.get("title") or "Public reply")
        if mid == thread:
            return "available", None, notification_message(client, post, mid, thread, title)
        if (thread, mid) not in originals:
            position, visited = None, set()
            batch = Batch()
            for _ in range(MAX_PAGES):
                items, following = page(client, "/posts/" + thread + "/comments", "comments", position)
                for original in descendants(items, batch):
                    comment_id = uuid(original["id"])
                    if comment_id == thread: raise ValueError("Comment shares root identity")
                    # Keep encountered parents for this context command. They must
                    # not need another scan against the same remaining time budget.
                    originals[thread, comment_id] = original
                    if comment_id == mid: break
                if (thread, mid) in originals: break
                if batch.error: return "unavailable", batch.error, None
                if following is None: return "missing", None, None
                cursor = following["cursor"]
                if cursor in visited: raise MailError("pagination_no_progress")
                visited.add(cursor)
                position = following
            else:
                return "unavailable", "budget_exhausted", None
        original = originals[thread, mid]
        if original.get("post_id") and uuid(original["post_id"]) != thread:
            raise ValueError("Unexpected thread")
        if original.get("is_deleted"): return "deleted", None, None
        if original.get("is_spam"): return "unavailable", "hidden_by_provider", None
        return "available", None, notification_message(client, original, mid, thread, title)
    except HTTPError as exc:
        if thread_loaded: return "unavailable", error_code(exc), None
        if root is None and exc.code == 404:
            exc.close()
            return "unknown", "thread_unknown", None
        if mid != thread and exc.code in (404, 410):
            code = "thread_missing" if exc.code == 404 else "thread_deleted"
            exc.close()
            return "unavailable", code, None
        return {404: "missing", 410: "deleted"}.get(exc.code, "unavailable"), error_code(exc), None
    except FAILURES as exc:
        return "unavailable", error_code(exc), None


def collect(settings, state, known, *, fetch=transport.fetch):
    """One pass over Moltbook. fetch asks the board: the transport, or an invented board in its place."""
    return adapter_notifications.collect(Client, NAME, settings, state, known, fetch)


def moltbook_reply(client, mid, thread, check):
    """Replies.read of Moltbook: the comment out of the comment tree of its thread, and the root of that thread
    is checked like the comment."""
    originals = {}
    status, error, _ = moltbook_lookup(client, mid, thread, originals=originals)
    if status != "available":
        raise MailError(error or "reply_" + status)
    original = originals[thread, mid]
    check(originals[thread, thread])
    return public_comment(original, top_by_depth=True)


BOARD = adapter_common.declare(
    NAME, partial(Client, NAME), collect,
    "Retained notifications and available comment trees in subscribed roots, with anonymous public originals. Reply/mention event variants remain provisional.",
    moltbook_lookup,
    dict(hosts=("www.moltbook.com", "moltbook.com"), pages=("post",), read=moltbook_reply, verified=True,
         explicit=("is_deleted", "is_spam")),
    partial(adapter_notifications.reference, HOST, Client.pages),
    keeps=True, root_as_thread=True, comment_by_thread=True)
