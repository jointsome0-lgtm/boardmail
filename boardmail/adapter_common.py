"""What the adapters of boards with an API key and a UUID for an account share. It names no board."""
from datetime import datetime
from http.client import HTTPException
import time
from urllib.error import HTTPError
from urllib.parse import urlencode

from . import addressing, transport
from .adapters import Batch, Board, Originals, Replies
from .errors import MailError, uuid

SHARED = dict(accept='application/json', cap=16 * 1024 * 1024, silence=10)
PAGE_SIZE = 100
MAX_PAGES = 100
SOURCE_SECONDS = 45
# A collection phase admits work for SOURCE_SECONDS. An HTTP request that it admits may take what is left of the
# phase, and never has less than this, so one admitted just before the phase ends still gets its answer. No
# further request starts after that boundary.
REQUEST_SECONDS = 10


class Client:
    host = None
    prefix = ""
    pause = 0
    request_seconds = None  # Remote lookup commands retain their existing shared deadline.

    def __init__(self, source, settings, *, fetch=transport.fetch):
        self.source, self.settings = source, settings
        self.owner = settings["account_id"]
        self.token = None
        self.deadline = time.monotonic()+SOURCE_SECONDS
        self.next_request = 0
        self.fetch = fetch

    def sign_in(self, key):
        """What authorizes a request, from the key of the account. Here the key itself."""
        return key

    def refused(self, exc, token, path):
        """The code of a refusal that the board explains, or None. Here it explains none."""
        return None

    def _request(self, path, *, token=None, body=None):
        if self.pause:
            time.sleep(max(0,self.next_request-time.monotonic()))
            self.next_request = time.monotonic()+self.pause
        remaining = self.deadline-time.monotonic()
        if remaining <= 0:
            raise MailError("budget_exhausted")
        try:
            return self.fetch(self.source, self.host+self.prefix+path,
                              left=remaining if self.request_seconds is None else max(remaining, self.request_seconds),
                              headers={"Authorization": "Bearer " + token} if token else None, body=body)
        except HTTPError as exc:
            code = self.refused(exc, token, path)
            if code:
                raise MailError(code) from None
            raise

    def get(self, path, params=None, *, authenticated=False):
        if authenticated and self.token is None:
            self.token = self.sign_in(transport.key(self.settings["api_key_file"]))
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


FAILURES = (MailError, OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError)
error_code = transport.failure


def failure(batch, exc):
    code = error_code(exc)
    batch.complete = False
    if code != "budget_exhausted" and (batch.error is None or code == "http_429"):
        batch.error = code
    return code


def collect(connect, settings, state, known, profile, mail, account=None):
    batch = Batch(state=state)
    known = set(known)  # The pass adds what it finds to a set of its own.
    try:
        client = connect()
        client.request_seconds = REQUEST_SECONDS
        profile = client.get(profile, authenticated=True)
        if profile.get("success") is False: raise ValueError("Invalid profile")
        account = account(profile) if account else profile
        if uuid(account["id"]) != uuid(settings["account_id"]):
            raise MailError("account_mismatch")
    except FAILURES as exc:
        return Batch(state=state, complete=False, error=error_code(exc))
    # Aliases come from the identity check already made; no extra profile request.
    configured = [*settings.get("mention_aliases", []), *settings.get("alias_search", [])]
    mention = addressing.mention_pattern(addressing.aliases(account, configured))
    try:
        mail(client, known, batch, mention)
    except FAILURES as exc:
        failure(batch, exc)
    return batch


def declare(name, client, collect, coverage, find, replies, reference, *fields, configure=None,
            parents_since_discovery=False, **asked):
    return Board(name=name, coverage=coverage, collect=collect, account=uuid, configure=configure,
                 fields=frozenset(("api_key_file", "mention_aliases", *fields)), required=frozenset(("api_key_file",)),
                 since_v1=True, originals=Originals(client, find, **asked), replies=Replies(client=client, **replies),
                 reference=reference, parents_since_discovery=parents_since_discovery)
