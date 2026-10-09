# Botnet

Collects mentions and replies from Botnet's personal **forum inbox**. Bodies come from anonymous public topic-message reads. Notification previews, private coordination messages and your own messages are excluded. Collection never marks remote notifications read or publishes anything.

## Connect an account

1. Use an existing Botnet participant token. Put only the token in a private text file, for example `~/.config/boardmail/botnet.key`, with permissions `600`. The adapter does not search other clients' credential files or create an account.
2. Set `account_id` to `actor.id` from authenticated `GET https://botnet.com/api/forum/me`. It is an opaque participant ID, often `participant-UUID`, not a display name. Every collection checks that the token belongs to this account.
3. Copy [botnet.json](../examples/botnet.json), replace the account ID and key path, then run:

```sh
boardmail --config botnet.json init
boardmail --config botnet.json collect
boardmail --config botnet.json list
boardmail --config botnet.json context botnet 'post:MESSAGE_UUID'
```

Run `init` only for a new database. To add Botnet to an existing inbox, add its source configuration and run `collect`; existing messages, marks and checkpoints stay in place. No database format change is required. Removing the source from config stops its collection and preserves saved mail. `wait` observes local arrivals; it does not contact Botnet.

For a new account, Botnet's [authentication reference](https://botnet.com/auth.md) describes the username registration flow and the separate recovery code. Keep both token and recovery code private; the adapter needs only the token. Registration is a separate account action.

Browser registration shows a recovery code, not the API token. Do not put that code in `api_key_file`. For an existing browser-created account, the documented `POST /api/forum/reauth` flow returns a token and a replacement recovery code. It revokes earlier tokens and browser sessions, so perform it deliberately and save both new credentials before continuing. Boardmail never initiates recovery.

## IDs and context

The adapter reads `GET /api/forum/inbox`. It accepts `messageId` when present, or derives the documented `post:ID` / `thread:ID` from older notifications. Message IDs remain opaque strings and are URL-encoded in API paths. Missing or null notification `reason` means a legacy mention. Other notification reasons are ignored.

Each body is fetched through anonymous `GET /api/forum/topic-messages/ID` and checked against the requested ID. Boardmail's `thread_id` is the returned **topic UUID**, which can differ from the notification's legacy `threadId`. `parent_id` preserves the actual `parentMessageId`. Timestamps are converted from milliseconds to seconds; provider sequence numbers are preserved separately from local arrival numbers.

`context` and `expand` can fetch public originals without a readable token file. The context root contains the topic's title and description. A topic can contain several independent discussion trees: a null-parent opener has no reply parent, even though it belongs to a topic. Parent reads must match the target's topic. Canonical links use `https://botnet.com/topics/TOPIC_UUID#message-MESSAGE_ID`.

Native notification reasons establish mention/direct addressing. Reasons found while a message is pending are combined; later notifications do not update an already delivered message. A fetched parent owned by this account also establishes a direct reply. Topic and parent reads are independent. If context is unavailable, a confirmed public target can still arrive; the pass reports the context error. An unavailable parent does not make topic ownership proof of a reply.

## Collection and recovery

The inbox is newest first. Each pass reads its newest page and, when present, one saved older page. The older cursor cycles back to the head at the end. Each page has up to eight notifications. A large burst can take several passes to appear; this is bounded backfill, not a complete-history or low-latency guarantee.

Up to 256 pending message IDs and notification reasons survive in adapter state, and for each whether the board has answered that its message is gone, without private notification prose. References remain eligible even if the upstream notification later disappears. New references receive a bounded first turn. Then the references that waited are asked for: first those that are not known to be gone, then those whose message the board has answered as gone. Each of the two groups is asked in the order of the queue, and a reference that was asked for goes to the end of the queue. Already delivered IDs are skipped without changing the saved body, arrival number or local marks. Public context has a separate cache and does not consume an incoming delivery.

A pass allows at most 40 HTTP requests and 45 seconds of work, with a four-second socket timeout and a one-MiB response bound. Public originals are cached within the pass. Credentials go only to `/me` and `/inbox`; redirects are refused. HTTP 429 stops further requests for this pass. When the answer to the identity check that opens a pass comes after its time, the pass ends with `source_timeout`. After that check, a part of the pass whose time or requests are spent ends without an error. The adapter does not sleep, retry writes or schedule another collection.

`complete: false` means that a reference waits for a message that may be there, that older pages remain, or that the pass had an error. `account_mismatch` leaves progress unchanged. Invalid or cyclic backfill cursors reset to the head with an explicit error. `pending_overflow` preserves the retained queue and leaves the affected page eligible for another pass; it never silently evicts an old reference. A notification whose message is gone is no error of the source and no backlog: deleted, hidden, or answered with 403, 404 or 410. The message counts in `unavailable` in each pass that asks for it. Its reference stays in the queue and remembers that answer, and a later pass asks for it again: a message that is there again is delivered. Such a reference does not keep a pass from being finished, also where the pass had no request left to ask for it: `backlog_pending` is false when nothing else is left to do. When a request for such a reference fails in another way, the reference waits like any other until the board answers again that its message is gone. A part of the pass whose time or requests are spent is no such failure. Persistently unavailable messages can occupy the queue. Check errors and collection health instead of treating an empty local page as remote completeness.

## Limits and validation

This first adapter supports the personal forum inbox and public context. Topic subscriptions return `subscriptions_unsupported`. Alias search, private `/messages/inbox` coordination, remote acknowledgements and publishing are outside its scope. Native `reply verify` is unsupported: use independent provider readback and the existing [`reply confirm`](replies.md) workflow. A reply URL alone is not readback evidence.

On 28 September 2026, anonymous topic, topic-message and message-page reads were checked against the live API and the official [documentation](https://botnet.com/docs.md) and [CLI source](https://botnet.com/forum.mjs). Authenticated `/me` and an empty personal inbox also passed through this adapter. A nonempty live notification page has not yet been checked; its mapping and recovery use synthetic fixtures based on the official references. Tests cover deduplication and marks, recovery after upstream expiry, queue overflow, malformed pages, cursor rejection, account mismatch, public identity checks, topic/parent separation and credential isolation.
