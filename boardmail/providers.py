"""Colony and Moltbook, read-only; confirmed prefixes survive failures with error health."""
import base64
import binascii
from functools import partial
import hashlib
import hmac
from http.client import HTTPException
import json
import time
from urllib.error import HTTPError

from . import adapter_common, adapter_notifications, transport
from .adapter_common import FAILURES, MAX_PAGES, PAGE_SIZE, error_code, text
from .adapter_notifications import descendants, notification_message, page
from .adapters import Batch, public_comment
from .errors import MailError, uuid

HOSTS = {"the-colony":"https://thecolony.ai", "moltbook":"https://www.moltbook.com"}
transport.BOARDS.update({
    'the-colony': transport.Board(protocol=None, **adapter_common.SHARED),
    'moltbook': transport.Board(protocol=None, **adapter_common.SHARED),
})
MAX_AUTH_ERROR_BYTES = 4096
COLONY_AUTH_CODES = frozenset({
    "AUTH_2FA_REQUIRED", "AUTH_2FA_INVALID", "AUTH_INVALID_TOKEN",
    "AUTH_TOKEN_REVOKED", "AUTH_PENDING_ACTIVATION", "AUTH_IP_DENIED", "AUTH_AGENT_ONLY",
})


def colony_auth_error(exc):
    """Read only a bounded, allowlisted reason; never retain provider prose."""
    try:
        raw = exc.read(MAX_AUTH_ERROR_BYTES + 1)
        if len(raw) > MAX_AUTH_ERROR_BYTES:
            return None
        data = json.loads(raw)
        detail = data.get("detail") if isinstance(data, dict) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        if isinstance(code, str) and code in COLONY_AUTH_CODES:
            return code.lower()
    except (OSError, HTTPException, ValueError, TypeError, AttributeError):
        pass
    finally:
        exc.close()
    return None


def colony_totp(secret_file):
    """Generate one SHA-1, 30-second, six-digit TOTP without storing its value."""
    try:
        with secret_file.open(encoding="ascii") as stream:
            raw = stream.read(129)
    except OSError:
        raise MailError("credentials_unavailable") from None
    except UnicodeError:
        raise MailError("invalid_totp_secret") from None
    if len(raw) > 128:
        raise MailError("invalid_totp_secret")
    secret = raw.strip()
    if not secret:
        raise MailError("credentials_unavailable")
    try:
        key = base64.b32decode(secret.upper() + "=" * (-len(secret) % 8))
        if not key:
            raise ValueError()
    except (binascii.Error, ValueError):
        raise MailError("invalid_totp_secret") from None
    counter = (int(time.time()) // 30).to_bytes(8, "big")
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 15
    number = int.from_bytes(digest[offset:offset + 4], "big") & 0x7fffffff
    return f"{number % 1_000_000:06d}"


class ColonyClient(adapter_notifications.Client):
    host = HOSTS["the-colony"]
    prefix = "/api/v1"
    profile = "/agents/me"
    kinds = {"comment_on_post": "reply_to_post", "reply_to_comment": "reply_to_comment", "mention": "mention"}
    event = ("notification_type", "post_id", "comment_id")
    reply, activity = "reply_to_comment", "comment_on_post"
    body, author = "body", "username"
    pages = "/posts/"

    def sign_in(self, key):
        body = {"api_key": key}
        if "totp_secret_file" in self.settings:
            body["totp_code"] = colony_totp(self.settings["totp_secret_file"])
        return self._request("/auth/token", body=body)["access_token"]

    def refused(self, exc, token, path):
        if (token or path == "/auth/token") and exc.code in (400, 401, 403):
            return colony_auth_error(exc)
        return None

    def page(self, path, key, position=None):
        """Comments come by page number, and notifications as a bare list from an offset."""
        params = {"limit": PAGE_SIZE, **(position or {})}
        if path.endswith("/comments"):
            number = params.get("page", 1)
            if type(number) is not int or number < 1: raise ValueError("Invalid page number")
            raw = self.get(path, {"limit": PAGE_SIZE, "page": number, "sort": "oldest"})
            items = raw["items"]
            if not isinstance(items, list): raise ValueError("Invalid page")
            more = raw["has_more"]
            if type(more) is not bool: raise ValueError("Invalid continuation")
            echoed = raw["page"]
            if type(echoed) is not int or echoed < 1: raise ValueError("Invalid page number")
            if echoed != number: raise MailError("pagination_no_progress")
            following = {"page": number+1}
        elif path == "/notifications":
            items = self.get(path, params, authenticated=True)
            if not isinstance(items, list): raise ValueError("Invalid page")
            # Do not assume the server honored the requested page size.
            more = bool(items)
            following = {"offset": params.get("offset", 0)+len(items)}
        else:
            return super().page(path, key, position)
        if more and (not items or following == position): raise MailError("pagination_no_progress")
        return items, following if more else None

    def comments(self, path, progress, batch):
        number = progress.get("page") if type(progress.get("page")) is int and progress.get("page") >= 1 else 1
        items, following = self.page(path, "comments", {"page": number})
        return items, None if following is None else {"page": following["page"]}


class MoltbookClient(adapter_notifications.Client):
    host = HOSTS["moltbook"]
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


def colony_lookup(client, mid, root=None):
    """Read an anonymous original, including an owner's comment absent from the inbox."""
    try:
        is_post = mid == root
        try:
            post = client.get(("/posts/" if is_post else "/comments/") + mid)
        except HTTPError as exc:
            # An unstored target can be a post or a comment. Known relatives have one endpoint.
            if root is not None or exc.code != 404: raise
            exc.close()
            post = client.get("/posts/" + mid)
            is_post = True
        if uuid(post["id"]) != mid: raise ValueError("Unexpected original")
        thread = mid if is_post else uuid(post["post_id"])
        if post.get("post_id") and uuid(post["post_id"]) != thread: raise ValueError("Unexpected thread")
        if root is not None and thread != root: raise ValueError("Unexpected thread")
        if post.get("is_deleted"): return "deleted", None, None
        if post.get("is_spam"): return "unavailable", "hidden_by_provider", None
        message = notification_message(client, post, mid, thread, text(post.get("title") or "Public reply"))
    except HTTPError as exc:
        return {404: "missing", 410: "deleted"}.get(exc.code, "unavailable"), error_code(exc), None
    except FAILURES as exc:
        return "unavailable", error_code(exc), None
    return "available", None, message


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


def colony_collect(settings, state, known, *, fetch=transport.fetch):
    """One pass over Colony. fetch asks the board: the transport, or an invented board in its place."""
    return adapter_notifications.collect(ColonyClient, "the-colony", settings, state, known, fetch)


def moltbook_collect(settings, state, known, *, fetch=transport.fetch):
    """One pass over Moltbook. fetch asks the board: the transport, or an invented board in its place."""
    return adapter_notifications.collect(MoltbookClient, "moltbook", settings, state, known, fetch)


def colony_reply(client, mid, thread, check):
    """Replies.read of Colony."""
    return public_comment(client.get("/comments/" + mid), body="body")


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


BOARDS = (
    adapter_common.declare(
        "the-colony", partial(ColonyClient, "the-colony"), colony_collect,
        "Retained reply/mention notifications and available comment pages in subscribed roots, confirmed against anonymous public originals. Retention is not guaranteed.",
        colony_lookup, dict(hosts=("thecolony.ai",), pages=("post", "posts"), read=colony_reply, explicit=("held",)),
        partial(adapter_notifications.reference, ColonyClient.host, ColonyClient.pages),
        "totp_secret_file", root_as_thread=True),
    adapter_common.declare(
        "moltbook", partial(MoltbookClient, "moltbook"), moltbook_collect,
        "Retained notifications and available comment trees in subscribed roots, with anonymous public originals. Reply/mention event variants remain provisional.",
        moltbook_lookup,
        dict(hosts=("www.moltbook.com", "moltbook.com"), pages=("post",), read=moltbook_reply, verified=True,
             explicit=("is_deleted", "is_spam")),
        partial(adapter_notifications.reference, MoltbookClient.host, MoltbookClient.pages),
        keeps=True, root_as_thread=True, comment_by_thread=True),
)
