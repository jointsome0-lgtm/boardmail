"""Invented API responses for offline examples/tests. Never contacts a board."""
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from uuid import UUID
import io


def uid(n):
    return str(UUID(int=n))


def settings():
    return {source:{"account_id":uid(n),"api_key_file":Path("unused-example.key"),
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
        self.fail = False
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

    def get(self, path, params=None, *, authenticated=False):
        params = dict(params or {})
        self.calls.append((path,params,authenticated))
        if self.fail:
            raise HTTPError("https://untrusted.invalid/secret-token",503,"private provider prose",{},io.BytesIO())
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
            end = start+params['limit']
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
            end = start+params['limit']
            return {"items" if colony else "comments":deepcopy(self.comments[start:end]),
                    "has_more":end<len(self.comments),"next_cursor":str(end)}
        return deepcopy(self.root) if colony else {"success":True,"post":deepcopy(self.root)}
