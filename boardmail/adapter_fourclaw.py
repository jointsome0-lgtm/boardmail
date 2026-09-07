"""Collect personal text from explicitly watched anonymous 4claw pages."""
from datetime import datetime
from html.parser import HTMLParser
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import UUID

from .adapters import Batch

API_VERSION = 1
HOST = "https://www.4claw.org"
MAX_BYTES = 2_000_000


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


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


def _fetch(thread):
    request = Request(f"{HOST}/t/{thread}", headers={"Accept": "text/html", "User-Agent": "boardmail/1"})
    deadline = time.monotonic() + 10
    with build_opener(_NoRedirect).open(request, timeout=5) as response:
        if response.headers.get_content_type() != "text/html":
            raise ValueError("format")
        content = bytearray()
        while True:
            if time.monotonic() >= deadline: raise TimeoutError()
            chunk = response.read1(min(65536, MAX_BYTES + 1 - len(content)))
            if time.monotonic() >= deadline: raise TimeoutError()
            if not chunk: break
            content.extend(chunk)
            if len(content) > MAX_BYTES: raise ValueError("size")
        return content.decode("utf-8")


def _messages(html, thread, account, aliases):
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
        if author.casefold() == account.casefold(): continue
        mention = any(re.search(r"(?<![\w@])@" + re.escape(alias) + r"(?!\w)", post["body"], re.I) for alias in aliases)
        if not mention and not (own_thread and not post["op"]): continue
        result.append({"id": thread if post["op"] else ids[position - 1],
                       "thread_id": thread, "kind": "reply_to_post" if own_thread and not post["op"] else "mention",
                       "parent_id": thread if own_thread and not post["op"] else None,
                       "author": author, "title": page.title, "body": post["body"],
                       "url": f"{HOST}/t/{thread}", "created_at": created})
    return result


def collect(settings, state, known):
    """One round-robin window, including failures, with constant-size progress."""
    try:
        account = settings["account_id"]
        threads = settings["watched_threads"]
        aliases = settings.get("mention_aliases", [account])
        if not isinstance(account, str) or not re.fullmatch(r"[A-Za-z0-9_]{2,64}", account): raise ValueError()
        if not isinstance(threads, list) or not 1 <= len(threads) <= 100: raise ValueError()
        if not all(isinstance(t, str) and str(UUID(t)) == t for t in threads): raise ValueError()
        threads = list(dict.fromkeys(threads))
        if not isinstance(aliases, list) or len(aliases) > 20: raise ValueError()
        if not all(isinstance(a, str) and re.fullmatch(r"[A-Za-z0-9_]{2,64}", a) for a in aliases): raise ValueError()
    except (KeyError, ValueError, TypeError, AttributeError):
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
            messages = _messages(_fetch(thread), thread, account, aliases)
            batch.messages.extend(m for m in messages if m["id"] not in known)
        except HTTPError as exc:
            exc.close()
            batch.error = f"http_{exc.code}" if exc.code in (401, 403, 404, 429, 500, 502, 503, 504) else "fourclaw_http_error"
            batch.unavailable += 1
        except (URLError, TimeoutError, OSError, HTTPException):
            batch.error = "fourclaw_network_error"
            batch.unavailable += 1
        except (ValueError, OverflowError, UnicodeError):
            batch.error = "fourclaw_invalid_public_page"
            batch.unavailable += 1
        checked += 1
    batch.state = {"next_thread": (offset + checked) % len(threads)}
    batch.complete = offset + checked >= len(threads) and batch.error is None
    return batch
