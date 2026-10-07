"""Invented API responses for offline examples/tests. Never contacts a board."""
from copy import deepcopy
from pathlib import Path
from typing import NamedTuple
from urllib.error import HTTPError
from urllib.parse import parse_qsl, urlsplit
from uuid import UUID
import io


def uid(n):
    return str(UUID(int=n))


def status(code, body=b""):
    """An answer of a FakeBoard: an HTTP status that is no success, with what the board says about it."""
    return HTTPError("https://board.example.invalid", code, "An invented status", {}, io.BytesIO(body))


class Asked(NamedTuple):
    """One request as a FakeBoard got it."""
    board: str     # the name of the board under which the client asked
    url: str
    headers: dict  # what the client sent besides the headers of the board
    body: object   # what a POST carried, or None
    left: object   # the seconds that the client said it had left, or None

    @property
    def params(self):
        """The query of the URL. Each value is text, as a board gets it."""
        return dict(parse_qsl(urlsplit(self.url).query))


class FakeBoard:
    """An invented board at the transport seam. A board module takes it where it would take
    boardmail.transport.fetch, and no request leaves the process.

    answers is what the board says. It is a function of the request, an Asked, or it is the answers themselves,
    which are then given out one by one in the order of the requests. An answer is what fetch() returns: what the
    JSON of the board says, or the text of its page. An answer that is an exception is raised, as fetch() raises
    it: status(503) for a status that is no success, an OSError for a board that is not reached, a ValueError for
    an answer that cannot be read.

    asked has every request so far."""
    def __init__(self, answers):
        self.answers = answers if callable(answers) else iter(answers)
        self.asked = []

    def __call__(self, board, url, *, left=None, headers=None, body=None):
        request = Asked(board, url, dict(headers or {}), body, left)
        self.asked.append(request)
        if callable(self.answers):
            answer = self.answers(request)
        else:
            try:
                answer = next(self.answers)
            except StopIteration:
                raise AssertionError("The board has no answer left for " + url) from None
        if isinstance(answer, BaseException):
            raise answer
        return deepcopy(answer)


# The key of every invented account, and what the invented Colony gives for it at sign-in.
KEY, TOKEN = "invented-key", "invented-token"
# Where the client asks each of the three boards. A message of a board is read at the host of this address.
ASKED_AT = {"postingboard":"https://getpostingboard.dev", "the-colony":"https://thecolony.ai/api/v1",
            "moltbook":"https://www.moltbook.com/api/v1"}


def settings(folder=None):
    """The three invented accounts. With a folder, each account has its key in a file there, as the client
    reads it before it asks a FixtureBoard. Without one the key file is not there, so a client that is handed no
    invented board stops before it asks a real one."""
    file = Path("unused-example.key")
    if folder is not None:
        file = Path(folder)/"example.key"
        file.write_text(KEY+"\n")
    return {source:{"account_id":uid(n),"api_key_file":file,
                    **({"threads":[uid(301),uid(302)],"mention_aliases":["@sample-agent"]}
                       if source=="postingboard" else {})}
            for n,source in enumerate(("the-colony","moltbook","postingboard"),1)}


def original(n, root, author=10, *, colony=False, body="A synthetic public reply."):
    return {"id":uid(n),"post_id":uid(root),"parent_id":None,
            "author":{"id":uid(author),"username" if colony else "name":"sample-writer"},
            "body" if colony else "content":body,"created_at":"2026-01-01T12:00:00Z"}


def named(n, root, author=10, *, body="A synthetic named-board reply.", reply_to=None):
    return {"id":uid(n),"root_id":uid(root),"thread_id":None if n==root else uid(root),
            "reply_to_id":uid(reply_to) if reply_to else None,
            "seq":n,"agent_id":uid(author),"author":"sample-writer","title":"Example discussion",
            "body":body,"created_at":1767268800+n}


def preview(post, **extra):
    """Inbox and search rows carry a preview, never the body."""
    item = {k:v for k,v in post.items() if k!="body"}
    item.update(preview=post["body"][:20],is_truncated=len(post["body"])>20,**extra)
    return item


class FixtureClient:
    def __init__(self, source, config):
        self.source, self.settings, self.owner = source,config,config["account_id"]
        self.host = "https://"+source+".example.invalid"
        self.calls = []
        self.fail = False     # True: every request gets a 503. An exception: every request ends with it.
        self.per_page = None  # The most that a page of notifications or comments holds. None: as many as asked.
        if source == "postingboard":
            self.roots = {uid(301):named(301,301,3),uid(302):named(302,302)}
            self.comments = {uid(301):[named(311,301),named(312,301)],uid(302):[
                named(313,302,3),named(314,302,body="@sample-agent, a result for you."),named(315,302)]}
            self.summaries = {uid(312)}
            self.others = {}   # Full posts outside watched roots, by ID.
            self.inbox = []    # (inbox_seq, post, reasons) addressed to the account.
            self.search = {}   # Query text to matching posts.
        else:
            colony = source=="the-colony"
            root = 101 if colony else 201
            self.root = {**original(root,root,int(UUID(self.owner)),colony=colony),
                         "title":"Example discussion"}
            self.comments = [original(root+10,root,colony=colony)]
            self.events = []
            for n,kind in ((root+10,"comment_on_post" if colony else "post_comment"),
                           (root+11,"mention")):
                self.events.append({"id":uid(n+1000),"notification_type" if colony else "type":kind,
                    "post_id" if colony else "relatedPostId":uid(root),
                    "comment_id" if colony else "relatedCommentId":uid(n),"is_read" if colony else "isRead":True})
            if colony:
                self.comments.append(original(root+11,root,colony=True))
            # Moltbook's second event intentionally has no public original yet.

    def failure(self):
        if isinstance(self.fail, BaseException):
            return self.fail
        return HTTPError("https://untrusted.invalid/secret-token",503,"private provider prose",{},io.BytesIO())

    def limit(self, params):
        return params["limit"] if self.per_page is None else min(params["limit"], self.per_page)

    def get(self, path, params=None, *, authenticated=False):
        params = dict(params or {})
        self.calls.append((path,params,authenticated))
        if self.fail:
            raise self.failure()
        if path == ("/v1/me" if self.source == "postingboard" else "/agents/me"):
            assert authenticated
            profile = {"id": self.owner}
            return {"agent": profile} if self.source == "moltbook" else profile
        if self.source == "postingboard":
            assert authenticated
            if path in ("/v1/inbox","/v1/search"):
                assert "before" not in params
                rows = ([preview(p,inbox_seq=s,reasons=r) for s,p,r in self.inbox] if path=="/v1/inbox"
                        else [preview(p) for p in self.search.get(params.get("q"),[])])
                key = "inbox_seq" if path=="/v1/inbox" else "seq"
                newer = sorted((r for r in rows if r[key]>params["after"]),key=lambda r:r[key])
                selected = newer[:params["limit"]]
                newest = selected[-1][key] if selected else params["after"]
                return {"items":list(reversed(selected)),"newest_cursor":newest,"resume_after":newest,
                        "next_after":newest if len(newer)>len(selected) else None,"next_before":None}
            key = path.rsplit("/",1)[-1]
            if key not in self.roots:
                post = next((p for items in self.comments.values() for p in items if p['id']==key),self.others.get(key))
                if post is None:
                    raise HTTPError("https://example.invalid",404,"absent",{},io.BytesIO())
                return {"post":deepcopy(post)}
            items = sorted(self.comments[key],key=lambda p:p['seq'],reverse=True)
            if "before" in params:
                items = [p for p in items if p['seq']<params['before']]
            selected = deepcopy(items[:params.get('limit',10)])
            for item in selected:
                if item['id'] in self.summaries:
                    del item['body']
            return {"post":deepcopy(self.roots[key]),"replies":{"items":selected,
                    "next_before":selected[-1]['seq'] if len(items)>len(selected) else None}}
        colony = self.source=="the-colony"
        if path=="/notifications":
            assert authenticated
            start = params.get("offset",0) if colony else int(params.get("cursor","0"))
            end = start+self.limit(params)
            selected = deepcopy(self.events[start:end])
            return selected if colony else {"notifications":selected,"has_more":end<len(self.events),"next_cursor":str(end)}
        assert not authenticated, "External public originals must be anonymous"
        if colony and path.startswith("/comments/"):
            comment = next((c for c in self.comments if c['id']==path.rsplit('/',1)[-1]), None)
            if comment is None:
                raise HTTPError("https://example.invalid",404,"absent",{},io.BytesIO())
            return deepcopy(comment)
        if path.endswith("/comments"):
            start = params.get("offset",0) if colony else int(params.get("cursor","0"))
            end = start+self.limit(params)
            return {"items" if colony else "comments":deepcopy(self.comments[start:end]),
                    "has_more":end<len(self.comments),"next_cursor":str(end)}
        return deepcopy(self.root) if colony else {"success":True,"post":deepcopy(self.root)}


class FixtureBoard(FixtureClient, FakeBoard):
    """The invented Postingboard, Colony or Moltbook at the transport seam: what FixtureClient answers, asked as
    the client of the board asks it. A collector takes it where it would take boardmail.transport.fetch.

    It answers where the board is asked and nowhere else, signs the account in to Colony, and takes the key or
    the token of the account as the sign that a request is authenticated. calls has what get() was asked, as
    with FixtureClient, and asked has every request with what it carried."""
    def __init__(self, source, config):
        FixtureClient.__init__(self, source, config)
        FakeBoard.__init__(self, self.answer)
        self.host = ASKED_AT[source].removesuffix("/api/v1")
        self.key = KEY    # The key of the account.
        self.totp = None  # The code that the account must send besides its key when it signs in to Colony.

    def answer(self, asked):
        assert asked.board == self.source and asked.url.startswith(ASKED_AT[self.source]+"/"), asked
        path, sent = urlsplit(asked.url).path[len(urlsplit(ASKED_AT[self.source]).path):], asked.headers.get("Authorization")
        if self.source == "the-colony" and path == "/auth/token":
            assert asked.body == {"api_key":self.key, **({"totp_code":self.totp} if self.totp else {})} and sent is None, asked
            if self.fail:
                raise self.failure()
            return {"access_token":TOKEN}
        assert asked.body is None and sent in (None, "Bearer "+(TOKEN if self.source == "the-colony" else self.key)), asked
        params = {name:value if name in ("cursor","q") or not value.isdigit() else int(value)
                  for name,value in asked.params.items()}
        return self.get(path, params, authenticated=sent is not None)


def together(boards):
    """One fetch for several invented boards, each under the name that its client asks it by."""
    return lambda name, url, **asks: boards[name](name, url, **asks)
