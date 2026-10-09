"""Adapter interface v1: collect(settings, state, known) returns a Batch."""
from collections.abc import Callable
from dataclasses import dataclass, field
import json
import re
from urllib.parse import urlsplit

from .errors import MailError, identifier, uuid


@dataclass
class Batch:
    """Public originals and resumable progress, committed together by the core.

    complete describes this adapter's scan, never complete remote history.
    Partial progress is normal; error is reserved for a failed operation.
    """
    messages: list = field(default_factory=list)
    state: dict = field(default_factory=dict)
    complete: bool = True
    error: str | None = None
    unavailable: int = 0
    originals: list = field(default_factory=list)


@dataclass(frozen=True)
class Originals:
    """How a command reads the originals of a board that lets it.

    client(settings, fetch=...) is the client of one command. find(client, mid, root) reads one original through
    it and gives its status, an error code and the message. message_id takes an id that the board can be asked
    for and raises ValueError for any other.

    The rest is how find is called. keeps: it also takes originals=, a dict of the command where it keeps what
    it has read. root_as_thread: a root that the inbox holds is asked for with itself as the thread, so that no
    comment is probed for. comment_by_thread: a comment that the inbox holds is asked for with its thread, where
    that is the only way to it.
    """
    client: Callable
    find: Callable
    message_id: Callable = uuid
    keeps: bool = False
    root_as_thread: bool = False
    comment_by_thread: bool = False


@dataclass(frozen=True)
class Replies:
    """How a reply that an account published on a board is found again and checked.

    A reference to a reply is an https address on one of hosts. direct is the path under which a reply has an
    address of its own, and pages are the first path segments of a thread page that holds a reply as the
    fragment comment-<id>. client is client(settings, *, fetch). read is read(client, mid, thread, check): it
    asks the board for the reply and returns the original, its thread, its author, what it answers or None for
    the thread itself, its text, and the basis on which the original could be read. Whatever else it reads on
    the way it hands to check, which raises where that is not plainly public. verified: an original counts only
    with the verification status verified. explicit: the fields that an original must give as false.
    """
    hosts: tuple
    client: Callable
    read: Callable
    direct: tuple = ()
    pages: tuple = ()
    verified: bool = False
    explicit: tuple = ()


def public_comment(original, body='content', top_by_depth=False):
    """What Replies.read returns for a comment that anyone can read, as three of the boards give one. body is
    the field of its text. top_by_depth: the board omits parent_id for a top-level comment and reports depth 0."""
    root = uuid(original['post_id'])
    author = uuid(original['author']['id'])
    if original.get('author_id') is not None and uuid(original['author_id']) != author:
        raise MailError('reply_author_mismatch')
    if top_by_depth and 'parent_id' not in original and type(original.get('depth')) is int and original['depth'] == 0:
        parent = None
    else:
        parent = original['parent_id']  # Missing relationship evidence must fail closed.
    return original, root, author, parent, original[body], 'anonymous_original'


@dataclass(frozen=True)
class Board:
    """What a board that ships with the package says of itself in its own module. boards.py lists them, and the
    core asks that list instead of comparing names.

    collect is collect(settings, state, known, *, fetch). fields are the settings that a config may give a
    source of the board besides account_id and adapter, and required are the ones that it must give. account
    takes an account id of the board and raises ValueError for any other value. configure, if the board has
    one, holds its settings to its own rules in place and raises ValueError where they do not fit. since_v1
    says that an inbox of schema v1, which has no adapter rows, holds the board under its own name.

    originals is how context and expand read an original, and None for a board that has no such lookup.
    replies is how a published reply is verified, and None for a board on which none can be. reference is
    reference(thread, parent): the identity under which a reply to parent is kept for a local join, never a URL
    to fetch. It raises ValueError for ids that are not the board's, and is None for a board that has no such
    identity.

    The last two are how its threads are read. rooted: a reply that names no parent answers the root of its
    thread. parents_since_discovery: a row with no discovery was stored before the reply targets of the board
    were kept, so only a fetched original says what it answers.
    """
    name: str
    coverage: str
    collect: Callable
    fields: frozenset = frozenset()
    required: frozenset = frozenset()
    account: Callable = identifier
    configure: Callable | None = None
    subscriptions: bool = True
    since_v1: bool = False
    originals: Originals | None = None
    replies: Replies | None = None
    reference: Callable | None = None
    rooted: bool = True
    parents_since_discovery: bool = False


def validate(batch):
    if not isinstance(batch, Batch) or not isinstance(batch.messages, list) or not isinstance(batch.state, dict) or not isinstance(batch.originals, list):
        raise MailError("invalid_adapter_result")
    if type(batch.complete) is not bool or type(batch.unavailable) is not int or batch.unavailable < 0:
        raise MailError("invalid_adapter_result")
    if batch.error is not None and (not isinstance(batch.error, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", batch.error)):
        raise MailError("invalid_adapter_result")
    try:
        json.dumps(batch.state, allow_nan=False)
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError):
        raise MailError("invalid_adapter_result") from None
    try:
        for item in batch.messages + batch.originals:
            for key in ("id", "thread_id"):
                identifier(item[key])
            if item.get("parent_id") is not None: identifier(item["parent_id"])
            if item.get("discovery") is not None and len(identifier(item["discovery"])) > 128: raise ValueError()
            for key in ("title", "body", "url"):
                if not isinstance(item[key], str): raise ValueError()
                item[key].encode("utf-8")
            if item.get("author") is not None:
                if not isinstance(item["author"], str): raise ValueError()
                item["author"].encode("utf-8")
            for key in ("created_at", "provider_seq"):
                if key == "provider_seq" and item.get(key) is None: continue
                if type(item[key]) is not int or not -(2**63) <= item[key] < 2**63: raise ValueError()
            url = urlsplit(item["url"])
            if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None:
                raise ValueError()
            url.port  # Validate a supplied port as well as the host.
        for item in batch.messages:
            if item["kind"] not in ("mention", "reply_to_post", "reply_to_comment", "thread_activity"):
                raise ValueError()
            if item.get("addressing") not in (None, "direct", "mention", "direct+mention", "thread"):
                raise ValueError()
    except (ValueError, TypeError, KeyError, AttributeError, OverflowError):
        raise MailError("invalid_adapter_result") from None
