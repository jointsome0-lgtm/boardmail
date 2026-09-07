"""Read-only adapters; confirmed prefixes survive failures with error health."""
from datetime import datetime
from http.client import HTTPException
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import MailError, uuid

HOSTS = {"postingboard":"https://getpostingboard.dev", "the-colony":"https://thecolony.ai",
         "moltbook":"https://www.moltbook.com"}
PAGE_SIZE = 100
MAX_PAGES = 100
SOURCE_SECONDS = 45
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise MailError("redirect_refused")


class Client:
    def __init__(self, source, settings):
        self.source, self.settings = source, settings
        self.owner = settings["account_id"]
        self.host = HOSTS[source]
        self.token = None
        self.deadline = time.monotonic()+SOURCE_SECONDS
        self.next_request = 0
        self.opener = build_opener(NoRedirect())

    def _request(self, path, *, token=None, body=None):
        if self.source == "postingboard":
            time.sleep(max(0,self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+1.1
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise MailError("source_timeout")
        headers = {"Accept":"application/json", "User-Agent":"boardmail/0.1"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if self.source == "postingboard":
            headers["X-Agent-Protocol"] = "getpostingboard/1"
        if body is not None:
            headers["Content-Type"] = "application/json"
        prefix = "" if self.source == "postingboard" else "/api/v1"
        request = Request(self.host+prefix+path, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
        with self.opener.open(request, timeout=min(10,remaining)) as response:
            chunks, size = [], 0
            while True:
                if time.monotonic() > self.deadline:
                    raise MailError("source_timeout")
                chunk = response.read1(65536)
                if not chunk:
                    return json.loads(b"".join(chunks))
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise MailError("response_too_large")
                chunks.append(chunk)

    def get(self, path, params=None, *, authenticated=False):
        if authenticated and self.token is None:
            try:
                key = self.settings["api_key_file"].read_text().strip()
                if not key:
                    raise ValueError()
            except (OSError, ValueError):
                raise MailError("credentials_unavailable") from None
            self.token = (self._request("/auth/token", body={"api_key":key})["access_token"]
                          if self.source == "the-colony" else key)
        return self._request(path+("?"+urlencode(params) if params else ""),
                             token=self.token if authenticated else None)


def timestamp(value):
    if type(value) is int:
        if not -(2**63) <= value <= 2**63-1:
            raise ValueError("Timestamp outside SQLite range")
        return value
    parsed = datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Missing timezone")
    return int(parsed.timestamp())


def text(value):
    if not isinstance(value,str):
        raise ValueError("Expected text")
    value.encode("utf-8")
    return value


def pages(client, path, key, params=None, *, authenticated=False):
    params = {**(params or {}),"limit":PAGE_SIZE}
    seen, continuations = set(), set()
    for _ in range(MAX_PAGES):
        page = client.get(path,params,authenticated=authenticated)
        bare = client.source == "the-colony" and path == "/notifications"
        items = page if bare else page[key]
        if not isinstance(items,list):
            raise ValueError("Invalid page")
        fresh = set()
        for item in items:
            item_id = uuid(item["id"])
            if item_id not in seen and item_id not in fresh:
                fresh.add(item_id)
                yield item
        seen.update(fresh)
        more = len(items)==PAGE_SIZE if bare else page["has_more"]
        if type(more) is not bool:
            raise ValueError("Invalid continuation flag")
        if not more:
            return
        if not fresh:
            raise MailError("pagination_no_progress")
        if client.source == "the-colony":
            params["offset"] = params.get("offset",0)+len(items)
        else:
            cursor = page["next_cursor"]
            if not isinstance(cursor,str) or not cursor or cursor in continuations:
                raise MailError("pagination_no_progress")
            continuations.add(cursor)
            params["cursor"] = cursor
    raise MailError("page_limit")


def descendants(comments):
    pending, seen = list(comments), set()
    while pending:
        comment = pending.pop()
        key = uuid(comment["id"])
        if key in seen:
            continue
        seen.add(key)
        yield comment
        replies = comment.get("replies",[])
        if not isinstance(replies,list):
            raise ValueError("Invalid replies")
        pending.extend(replies)


def notification_mail(client, known, mail):
    colony = client.source == "the-colony"
    kinds = ({"comment_on_post":"reply_to_post", "reply_to_comment":"reply_to_comment", "mention":"mention"}
             if colony else {"post_comment":"reply_to_post", "comment_reply":"reply_to_comment", "mention":"mention"})
    wanted = {}
    for event in pages(client,"/notifications","notifications",authenticated=True):
        kind = kinds.get(event.get("notification_type" if colony else "type"))
        if kind is None:
            continue
        raw_post_id = event.get("post_id" if colony else "relatedPostId")
        if raw_post_id is None and kind == "mention":
            continue  # Mentions in non-post surfaces are outside public thread mail.
        post_id = uuid(raw_post_id)
        comment_id = event.get("comment_id" if colony else "relatedCommentId")
        message_id = uuid(comment_id) if comment_id else post_id
        if message_id not in known:
            ids = wanted.setdefault(post_id,{})
            if message_id not in ids or kind == "mention":
                ids[message_id] = kind
    unavailable = 0
    for post_id, ids in wanted.items():
        try:
            raw = client.get("/posts/"+post_id)
        except HTTPError as exc:
            # Only the root lookup establishes absence. A failed later page is
            # incomplete collection, even when its HTTP status is 404.
            if exc.code not in (403,404,410):
                raise
            exc.close()
            unavailable += len(ids)
            continue
        post = raw if colony else raw["post"]
        if uuid(post["id"]) != post_id:
            raise ValueError("Unexpected post")
        title = text(post["title"])
        found = set()

        def accept(original):
            message_id = uuid(original["id"])
            if message_id not in ids or message_id in found:
                return
            if original.get("post_id") and uuid(original["post_id"]) != post_id:
                raise ValueError("Unexpected comment thread")
            if original.get("is_deleted") or original.get("is_spam"):
                return
            author = original.get("author")
            if author is None:
                author = {}
            if not isinstance(author, dict):
                raise ValueError("Invalid author")
            if author.get("id") and uuid(author["id"]) == client.owner:
                found.add(message_id)
                return
            name = author.get("username" if colony else "name")
            if name is not None:
                name = text(name)
            url = client.host+("/posts/" if colony else "/post/")+post_id
            if colony and message_id != post_id:
                url += "#comment-"+message_id
            mail.append({"id":message_id,"thread_id":post_id,"kind":ids[message_id],
                         "parent_id":uuid(original["parent_id"]) if original.get("parent_id") else None,
                         "author":name,"title":title,
                         "body":text(original["body" if colony else "content"]),"url":url,
                         "created_at":timestamp(original["created_at"])})
            found.add(message_id)

        accept(post)
        if ids.keys()-{post_id}:
            for comment in pages(client,"/posts/"+post_id+"/comments","items" if colony else "comments",
                                 {"sort":"oldest" if colony else "old"}):
                for original in descendants([comment]):
                    accept(original)
        unavailable += len(ids.keys()-found)
    return unavailable


def postingboard_mail(client, known, mail):
    aliases = client.settings.get("mention_aliases",[])
    mention = re.compile(r"(?<![\w-])(?:"+"|".join(re.escape(a) for a in aliases)+r")(?![\w-])",re.I) if aliases else None
    unavailable, error = 0, None
    for thread_id in client.settings["threads"]:
        client.deadline = time.monotonic()+SOURCE_SECONDS
        try:
            unavailable += postingboard_thread(client, known, mail, thread_id, mention)
        except (MailError,OSError,HTTPException,ValueError,KeyError,TypeError,AttributeError) as exc:
            if isinstance(exc, HTTPError):
                exc.close()
            if error is None:
                error = exc
    if error is not None:
        raise error
    return unavailable


def postingboard_thread(client, known, mail, thread_id, mention):
    path = "/v1/posts/"+thread_id
    try:
        first = client.get(path,{"limit":30},authenticated=True)
    except HTTPError as exc:
        if exc.code != 404:
            raise
        exc.close()
        return 1
    root, page = first["post"], first["replies"]
    if uuid(root["id"]) != thread_id:
        raise ValueError("Unexpected root")
    own_root = root.get("agent_id") is not None and uuid(root["agent_id"]) == client.owner

    def accept(post):
        key = uuid(post["id"])
        if key in known or (post.get("agent_id") is not None and uuid(post["agent_id"]) == client.owner):
            return
        body = text(post["body"])
        kind = "mention" if mention and mention.search(body) else (
            "reply_to_post" if own_root and key != thread_id else None)
        if kind is None:
            return
        seq = post["seq"]
        if type(seq) is not int or not 0 <= seq <= 2**63-1:
            raise ValueError("Invalid sequence")
        author = post.get("author")
        if author is not None:
            author = text(author)
        mail.append({"id":key,"thread_id":thread_id,"provider_seq":seq,"kind":kind,
                     "author":author,"title":text(root.get("title") or "Untitled thread"),
                     "body":body,"url":client.host+"/v1/posts/"+key,"created_at":timestamp(post["created_at"])})

    accept(root)
    seen, cursors = {thread_id}, set()
    for _ in range(MAX_PAGES):
        items = page["items"]
        if not isinstance(items,list):
            raise ValueError("Invalid replies")
        fresh = False
        for item in items:
            key = uuid(item["id"])
            if key in seen:
                continue
            seen.add(key)
            fresh = True
            if key in known:
                continue
            if "body" not in item:
                item = client.get("/v1/posts/"+key,authenticated=True)["post"]
            if uuid(item["id"]) != key or uuid(item["root_id"]) != thread_id:
                raise ValueError("Unexpected reply")
            accept(item)
        before = page["next_before"]
        if before is None:
            break
        if not fresh or type(before) is not int or before in cursors:
            raise MailError("pagination_no_progress")
        cursors.add(before)
        page = client.get(path,{"limit":30,"before":before},authenticated=True)["replies"]
    else:
        raise MailError("page_limit")
    return 0


def collect_all(store, sources, *, client_factory=Client):
    added, failed = 0, False
    for source, settings in sources.items():
        batch = []
        try:
            known = store.known(source,settings["account_id"])
            client = client_factory(source,settings)
            unavailable = (postingboard_mail(client,known,batch) if source == "postingboard"
                           else notification_mail(client,known,batch))
            added += store.save(source,settings["account_id"],batch,unavailable=unavailable)
        except (MailError,OSError,HTTPException,ValueError,KeyError,TypeError,AttributeError) as exc:
            if isinstance(exc,MailError):
                error = str(exc)
            elif isinstance(exc,HTTPError):
                error = "http_"+str(exc.code)
                exc.close()
            elif isinstance(exc,(TimeoutError,URLError,OSError,HTTPException)):
                error = "network_error"
            else:
                error = "invalid_response"
            if batch:
                added += store.save(source,settings["account_id"],batch,error=error)
            else:
                store.failure(source,settings["account_id"],error)
            failed = True
    return {"event":"collected","added":added,"failed":failed,**store.status()}
