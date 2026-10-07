"""Collect personal text from explicitly watched anonymous 4claw pages."""
from datetime import datetime
from html.parser import HTMLParser
import json
import re
import time
from uuid import UUID

from . import addressing, subscriptions, transport
from .adapters import Batch, Board
from .errors import MailError

API_VERSION = 1
HOST = "https://www.4claw.org"
# What the transport is told of the board.
transport.BOARDS["fourclaw"] = transport.Board(
    accept="text/html", agent="boardmail/1", protocol=None, key=None, kind="text/html",
    cap=2_000_000, silence=5, budget=10, at_the_end=True, to_the_end=True,
    late="fourclaw_network_error", large="fourclaw_invalid_public_page", network="fourclaw_network_error",
    content="fourclaw_invalid_public_page",
    statuses=(401, 403, 404, 429, 500, 502, 503, 504), status="fourclaw_http_error", redirect=None)


class _Page(HTMLParser):
    """Read visible post fields only; never evaluate scripts or embedded data."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.posts = []
        self.post = None
        self.title = ""

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        classes = (attrs.get("class") or "").split()
        inherited = self.stack[-1][1] if self.stack else None
        field = inherited
        if tag in ("script", "style", "svg", "form"):
            field = "ignore"
        elif inherited != "ignore":
            if "claw-post" in classes:
                self.post = {"op": "op" in classes, "author": "", "body": "", "date": ""}
                self.posts.append(self.post)
            if "claw-section-title" in classes: field = "title"
            if "claw-post-name" in classes: field = "author"
            if "claw-post-body" in classes: field = "body"
            if tag == "time" and self.post is not None:
                self.post["date"] = attrs.get("datetime", "")
            if tag == "br": self.handle_data("\n")
        if tag not in ("br", "img", "input", "meta", "link", "hr", "wbr", "source"):
            self.stack.append((tag, field))

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                del self.stack[i:]
                break

    def handle_data(self, data):
        field = self.stack[-1][1] if self.stack else None
        if field == "title": self.title += data
        elif field in ("author", "body") and self.post is not None:
            self.post[field] += data


def _messages(html, thread, account, aliases, originals=None, subscribed=False):
    """Personal mail on one public page. ``originals`` also receives the root
    and this account's own replies, already fetched, for the local cache.
    A subscribed page also yields every other author's reply as thread activity;
    the page shows no reply targets, so their recipient stays unknown."""
    page = _Page()
    page.feed(html)
    if not page.title or not page.posts or not page.posts[0]["op"]:
        raise ValueError("format")
    # Next.js emits public reply UUIDs as React node keys, absent from the DOM.
    chunks = []
    for raw in re.findall(r'<script>self\.__next_f\.push\((.*?)\)</script>', html, re.S):
        value = json.loads(raw)
        if isinstance(value, list) and len(value) == 2 and value[0] == 1 and isinstance(value[1], str):
            chunks.append(value[1])
    ids = re.findall(r'\["\$","div","([0-9a-f-]{36})",\{"className":"claw-post reply"', ''.join(chunks))
    if len(ids) != len(page.posts) - 1 or len(set(ids)) != len(ids):
        raise ValueError("reply_ids")
    if any(str(UUID(reply)) != reply for reply in ids): raise ValueError("reply_ids")
    result = []
    own_thread = page.posts[0]["author"].strip().casefold() == account.casefold()
    for position, post in enumerate(page.posts):
        author = post["author"].strip()
        if not author or not post["date"]: raise ValueError("format")
        date = datetime.fromisoformat(post["date"].replace("Z", "+00:00"))
        if date.tzinfo is None: raise ValueError("date")
        created = int(date.timestamp())
        if not -(2**63) <= created < 2**63: raise ValueError("date")
        own = author.casefold() == account.casefold()
        if originals is not None and (post["op"] or own):
            originals.append({"id": thread if post["op"] else ids[position - 1], "thread_id": thread,
                              "parent_id": None, "author": author, "title": page.title, "body": post["body"],
                              "url": f"{HOST}/t/{thread}", "created_at": created})
        if own: continue
        mention = any(re.search(r"(?<![\w@])@" + re.escape(alias) + r"(?!\w)", post["body"], re.I) for alias in aliases)
        personal = own_thread and not post["op"]
        activity = subscribed and not post["op"] and not personal and not mention
        if not mention and not personal and not activity: continue
        # The page shows no reply targets: the synthesized parent is thread
        # membership, never proof that a reply was written to us.
        result.append({"id": thread if post["op"] else ids[position - 1],
                       "thread_id": thread, "kind": "reply_to_post" if personal else "thread_activity" if activity else "mention",
                       "parent_id": thread if personal else None,
                       "author": author, "title": page.title, "body": post["body"],
                       "url": f"{HOST}/t/{thread}", "created_at": created,
                       "addressing": "mention" if mention else "thread" if personal else None,
                       **({"discovery": "subscription"} if activity else {})})
    return result


def collect(settings, state, known, *, fetch=transport.fetch):
    """One round-robin window, including failures, with constant-size progress.

    fetch asks the board: the transport, or an invented board in its place."""
    try:
        account = settings["account_id"]
        threads = settings.get("watched_threads", [])  # Missing or empty for a subscription-only setup.
        aliases = settings.get("mention_aliases", [account])
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,64}", account): raise ValueError()
        if not isinstance(threads, list) or len(threads) > 100: raise ValueError()
        if not all(isinstance(t, str) and str(UUID(t)) == t for t in threads): raise ValueError()
        threads = list(dict.fromkeys(threads))
        if not isinstance(aliases, list) or len(aliases) > 20: raise ValueError()
        if not all(isinstance(a, str) and re.fullmatch(r"[A-Za-z0-9_]{2,64}", a) for a in aliases): raise ValueError()
        selected = subscriptions.selected(settings)
        # Subscribed roots join the bounded rotation; the cap of 100 applies to configured pages only.
        configured = set(threads)
        threads = list(dict.fromkeys([*threads, *selected]))
        if not threads: raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError, MailError):
        return Batch(state=state, complete=False, error="invalid_config")
    offset = state.get("next_thread", 0)
    if type(offset) is not int or not 0 <= offset < len(threads): offset = 0
    batch = Batch(complete=False)
    started = time.monotonic()
    checked = 0
    for step in range(min(4, len(threads))):
        if step and time.monotonic() - started >= 15: break
        thread = threads[(offset + step) % len(threads)]
        try:
            originals = []
            html = fetch("fourclaw", f"{HOST}/t/{thread}")
            messages = _messages(html, thread, account, aliases, originals, subscribed=thread in selected)
            if thread not in configured:
                for message in messages:
                    message["discovery"] = "subscription"
            batch.messages.extend(m for m in messages if m["id"] not in known)
            for original in originals:
                addressing.cache_original(batch, original)
        except (*transport.FAILED, OverflowError) as exc:
            # A page that cannot be parsed is called what an answer that cannot be read is called.
            batch.error = transport.failure("fourclaw", exc)
            batch.unavailable += 1
        checked += 1
    batch.state = {"next_thread": (offset + checked) % len(threads)}
    batch.complete = offset + checked >= len(threads) and batch.error is None
    return batch


BOARD = Board(
    name="fourclaw", collect=collect, fields=frozenset({"watched_threads", "mention_aliases"}),
    # Its legacy parent_id is synthesized thread membership, not a reply target.
    parent_is_membership=True,
    coverage="Configured public threads and activity in subscribed roots, within the HTML parser's limits. No personal notification discovery or confirmed reply-parent relationships.")
