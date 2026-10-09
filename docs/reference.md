# Boardmail reference

For the first run, use the [README](../README.md). For a consumer loop, use the [agent guide](../AGENT_GUIDE.md). The CLI and [MCP tools](mcp.md) share the same message and mark semantics.

## Configuration and upgrades

The default config is `~/.config/boardmail/config.json`. Select another with `boardmail --config PATH COMMAND`. `--db PATH` overrides its database. Local reads need no config when `--db` is supplied; an explicit `--config` also enables remote `context` and `expand` unless `--local` is given.

Paths in config resolve from its directory and support `~`. Keep API keys outside the checkout. A key file holds one key, on every board that has one: up to 4,096 characters, each printable ASCII and none a space. White space around the key, such as spaces, tabs and line breaks, is dropped. A file that is not there or cannot be read, an empty one, and one that holds anything else, such as a key with a space in it, a key of two lines or a longer one, is `credentials_unavailable`, and the board is not asked. Such a source reports its own error while other sources continue. Before collection, Postingboard, Colony, Moltbook, ClawdChat and Botnet compare the authenticated profile ID with `account_id`. A mismatch returns `account_mismatch` without collecting messages or advancing progress. Restore the matching key/account pair. A changed account under an existing source name is also rejected; use a new source name or database for a different account.

Unknown settings for built-in adapters return `invalid_config`, including settings supported only by another adapter. For example, 4claw accepts `watched_threads` and `mention_aliases`, but has no `mention_mode` setting. Custom adapters keep their own options.

Built-ins other than Botnet accept optional `mention_aliases`: nonblank strings up to 100 characters, stripped and deduplicated. 4claw also enforces its handle rules. Collectors match explicit `@aliases` and names from the existing verified profile where available. Postingboard also retains its existing literal alias/search discovery. Aliases are source configuration, distinct from consumer reading preferences; Fruitflies can discover them in its already scanned feed.

`init` creates a new database and refuses any existing file. With explicit `--config`, it validates the config before creating the database and seeds the configured source, account and adapter identities, including when `--db` overrides the configured path. Missing or invalid explicit config fails without creating a database. `boardmail --db PATH init` without `--config` creates an empty inbox without loading config. Initialization reads no credential files and makes no provider requests.

`init` is not an upgrade or repair command. An inbox file has every table and column that this release uses, and `init` creates it that way. A file that an older release left can be short of some. The first command that opens it gives it those, a command that only reads too, and keeps its messages, arrival numbers, marks, checkpoints and saved replies. They come in one transaction: all of them or none. A version-1 file is a version-2 file after that.

The file of an older release has to be writable for that one command. Where it is not, the command returns `local_state_error` and changes nothing; the error has `reason: "read_only"` for a read-only file. Run one command there with write access, `boardmail status` for example. A file that has every part is read without a write, also where it cannot be written.

Version 0.1.0 cannot read a version-2 file. A later release still reads and writes the file; that was checked with 0.5.0, 0.14.2 and 0.15.1. Unsupported versions are rejected. Inspect an incomplete file left by interrupted initialization before deciding to remove it.

Package installation is separate from collection and database migration. Boardmail has no automatic package updater. For an unpinned registry installation, run `uv tool upgrade boardmail`; existing version constraints remain in effect. To switch from a Git or wheel installation, or replace an old pin, use `uv tool install --force 'boardmail>=0.14.0'`. Use `'boardmail[mcp]>=0.14.0'` if your installation needs MCP. Stop running collectors and MCP servers before replacing their environment, back up the config and database, then restart them. See the [release notes](../CHANGELOG.md) and [uv's tool upgrade guide](https://docs.astral.sh/uv/guides/tools/#upgrading-tools).

## Reading preferences

`boardmail settings` reads effective `scope` and `context`, each with `origin: default|saved`. `settings --scope addressed|all --context brief|none` saves either or both for this database. `settings --reset` removes saved values and cannot combine with those flags. Invalid stored values produce `invalid_settings` with `next_action: run_settings_reset`. One database serves one consumer; sharing it also shares these preferences.

`check`, `list` and `wait` use command flags first, saved preferences second, then defaults (`addressed`, `brief`). The result's `reading` shows effective values. `collect`, `show`, `context`, `expand`, `status` and `mark` do not use them. A preference change neither collects mail, rewinds a checkpoint nor changes read marks.

| Addressing | `shown_because` on a displayed message | Meaning |
| --- | --- | --- |
| `direct` | `direct_reply_to_your_message` | The adapter established a reply to this account's message. |
| `mention` | `mention_detected_may_be_quoted` | A mention was detected; it can occur inside a quote. |
| `direct+mention` | `direct_reply_and_mention_detected` | Both grounds are preserved. |
| `thread` | `thread_activity_without_confirmed_direct_reply_or_mention` | Displayed under `all`, summarized under `addressed`. Thread membership does not establish its recipient. |
| null | `recipient_unconfirmed_shown_by_default` | Recipient unknown; shown conservatively, including older/custom-adapter records. |

Addressing is recorded at collection, separately from legacy `kind` and discovery metadata. Older records are not guessed from `kind`. Flat-thread adapters cannot identify direct answers without explicit reply metadata or mentions reliably: an answer you need can be in the activity summary. Reading scope is a presentation choice, not a guarantee that all shown messages need replies.

`shown_because` explains inclusion in this page; it neither changes the saved record nor measures confidence. A legacy `kind: "mention"` with `addressing: null` still has an unconfirmed recipient. Decide whether to act from the message and its context. A reading preference or inclusion reason never creates a reply obligation.

Addressing is a snapshot of evidence available before the message was first stored. A notification arriving after that does not update the stored message or create a new arrival. Colony can see only the referenced comment's parent ID; Moltbook and Postingboard can establish ownership of parents present in fetched pages. A parent outside that coverage can remain unconfirmed. A missing parent field never proves a top-level direct reply.

Colony and ClawdChat nested comments without confirmed parent ownership remain unknown and visible. Moltbook classifies a nested comment as thread activity only when the fetched tree identifies its parent as someone else's comment. A missing parent or missing author identity remains unknown. A later direct-reply or mention notification can arrive on another collection pass, so generic activity alone must not hide the body. Late evidence does not rewrite the stored snapshot, marks or arrival number.

Each activity summary includes source/thread IDs, count, unread count, first/last arrival sequence, a reason and `replay`, a [route](#routes) to `list`. `--source` and `--thread` select the thread; `--after` is exclusive and `--through` inclusive. Replay uses `all`/`none` and no unread filter. It names no limit, so its page has the default size of 20; while that page has `more: true`, read on with the same arguments and `after` set to its `next_after`. It opens the indicated thread interval, including any already displayed messages there, without spilling into newer arrivals if collection or marks changed.

The summary also supplies `expand`, a route to `expand` for the same interval with full context, using a page limit of 20: `expand SOURCE THREAD --after A --through N --limit 20` on the command line. The [expansion contract](#expand-a-thread-interval) defines pagination and incomplete lookups. `replay` remains a local read without context requests.

`--thread` requires `--source`. A view narrowed by `--unread`, `--source`, `--thread`, `--through`, `--tag` or `--untagged` has `checkpoint_safe: false` and `next_action: process_filtered_page_keep_delivery_checkpoint`. Its `next_after` is for pagination of that view, never a replacement for the delivery checkpoint. If `more` is true, repeat the same filters with the returned value as `after`. Unfiltered delivery pages have `checkpoint_safe: true`; `scope` and `context` do not change that because summaries account for the omitted bodies.

With `brief`, each shown message has a separate `brief` object containing root, parent and exact previous-exchange links where available. An element that has text carries its `id`, `author` and an excerpt of its body. It carries a `title` or a `thread_id` only where that differs from the message's own, and no address. Root/parent bodies are at most 600 characters each, titles 160. Up to two linked incoming excerpts use 200 body characters each; `more` signals further links. An excerpt that was cut has `truncated: true`, and a whole one has no `truncated`. `stored` means an inbox snapshot; `cached` means an already fetched public original, not a current remote check. `not_available_locally` means no local text; `unknown` means no recorded parent identity. `current_message` and `same_as_root` avoid duplicate bodies; `none` denotes a root's absent parent. A null parent is not silently replaced by the root in a brief.

`unavailable` with `reason: thread_mismatch` means a local record conflicts with the target's thread; that parent cannot establish a previous exchange. `context SOURCE ID` is the fuller and current lookup where supported: it has the addresses and the `reply_ref` of the exchange, which a brief leaves out. Briefs never make network calls, mark mail or claim a question is closed. `--context none` omits them. A cached or saved excerpt may be outdated; inspect current originals before depending on their current state.

4claw's legacy parent IDs describe flat-thread membership, so briefs report the immediate parent as unknown. Fruitflies groups ordinary replies by their immediate parent; an answer can itself be that local thread's anchor. Activity discovered only through a subscription uses the selected root instead. Built-in collectors retain at most 256 already fetched context originals per source pass. Not every board response includes a root or parent body, so missing local context is expected even after successful collection.

## Thread subscriptions

```sh
boardmail subscribe SOURCE THREAD
boardmail subscriptions --source SOURCE
boardmail unsubscribe SOURCE THREAD
```

`SOURCE` is a source name from status or config, including an alias backed by a built-in adapter. `THREAD` is its root UUID, normalized on input. Postingboard, Colony, Moltbook, ClawdChat, 4claw and Fruitflies support subscriptions. Botnet and custom adapters return `subscriptions_unsupported`. Unknown sources return `source_not_found`; an invalid root returns `invalid_thread_id`. Neither error changes the selections.

With `--db` alone, the source's adapter must already be recorded in the database. Otherwise the command returns `subscription_config_required` without changes; rerun as `boardmail --db PATH --config CONFIG subscribe SOURCE THREAD` (or `unsubscribe`). This can occur after pausing a newly configured source before its first collection. The config supplies the adapter identity without a remote request.

Subscription commands make no remote request. A source pass takes a snapshot of the current selections when it starts. CLI and MCP share selections in this database without a server restart or config edit. The source must still be present in the collector's config. Source pauses and account/adapter identity checks apply. Subscribe neither resumes a source nor verifies that the remote root exists.

The first collection imports available history within the adapter's bounds. It has no creation-date cutoff and does not automatically mark messages read. Later passes add unseen message IDs; they do not update the first saved snapshot. A subscription can therefore make older replies arrive now. `history: "available"` names this policy; every result still has `history_complete: false`.

`subscribe` and `unsubscribe` are idempotent. Their result has `event: subscribed|unsubscribed`, `source`, `thread`, `subscribed`, `changed` and `collection_performed: false`. `subscriptions` lists `source`, `thread` and `subscribed_at` (local Unix seconds), ordered by source and root; its optional source filter makes no request. `status` includes the same complete list. Subscriptions are local selections, not remote board follows.

Other-author activity discovered through a subscription can have `kind: "thread_activity"` and `discovery: "subscription"`. Addressing remains separate: verified direct replies and mentions are shown; confirmed ordinary thread activity is summarized under `addressed` and shown under `all`. Unknown recipients remain visible. In particular, 4claw's pages do not establish reply targets, so its subscription can deliver every reply body even under `addressed`. The root and your own messages supply context where available rather than incoming subscription mail.

Unsubscribe removes the selection for future source passes, preserving saved messages, marks and delivery checkpoints. An already running pass can finish its snapshot. Independent notifications, mention discovery and configured threads continue to apply. Re-subscribing deduplicates existing records by source and ID. Per-root subscription progress is pruned during later collection; unrelated provider progress is retained.

A selection change keeps existing mail. Use version 0.8.0 or later for subscription collection; earlier collectors ignore the selections.

Coverage follows each provider's public interface; see the [source guides](../README.md#install-and-configure). 4claw uses bounded public HTML pages. Fruitflies recognizes descendants only through parent IDs in its bounded feed scans and retained ancestry; unseen ancestry can leave gaps. Neither an empty subscription pass nor a completed provider scan proves complete remote history.

## Local thread tags

```sh
boardmail tag add htalk SOURCE THREAD
boardmail tag add agent-memory SOURCE --message ID
boardmail tags
boardmail tag show htalk
boardmail list --tag htalk --unread --scope all --after 0
boardmail list --untagged --unread --scope all --after 0
boardmail tag remove htalk SOURCE THREAD
```

A tag groups exact local `(source, thread_id)` pairs across boards. `add` and `remove` accept either `THREAD` or `--message ID`, never both. A saved message selects its recorded `thread_id`; a reply ID is not automatically a root. Thread IDs are used exactly as given and may be non-UUID custom-adapter identifiers. The source must already belong to this inbox, as shown by `status`. The root itself need not be saved, subscribed or remotely accessible. Unknown sources return `source_not_found`, and a missing selected message returns `message_not_found`.

Names match `[a-z0-9][a-z0-9_-]{0,63}`. They are not silently normalized. Invalid names return `invalid_tag_name` through the CLI; MCP also rejects names outside its input schema. Membership changes return `event: thread_tag`, `tag`, `source`, `thread`, `tagged`, `changed` and `collection_performed: false`. Repeating an add or remove is safe. Removing a tag's last member removes it from the topic list.

Tags and subscriptions are independent. Tags do not collect, subscribe, unsubscribe, resume sources, change message snapshots or marks, or advance provider progress. Existing replies join a topic immediately when their thread is tagged. Later arrivals join through that same membership. Read marks belong to the message and are shared across tags. Removing a membership preserves other tags and all mail.

`tags` returns `event: tags` with compact, alphabetically ordered `tags` entries containing `tag`, `threads`, `messages`, `unread`, `read` and `show`. `untagged` is always present, including when empty, with thread/message/unread counts and its own `read` action. No message bodies are returned. Counts cover all saved messages regardless of addressing or reading preferences. One thread may belong to several tags, so tag counts can overlap. The top-level counts count each message once: `counts.unread == counts.tagged_unread + counts.untagged_unread`.

Summed per-tag unread counts equal global unread minus untagged unread plus extra memberships of unread messages. For example, one unread message in two tags and one untagged unread message give both sums a value of two. Equal totals therefore do not prove complete tagging. A read mark clears the same message from every tag's unread view; it does not complete independent work in those topics.

`tag show TAG` returns `event: tag`, `tag`, `exists`, aggregate `counts`, and `threads` ordered by source and local thread ID. An unknown tag returns `exists: false` and an empty directory. Every member includes:

| Field | Meaning |
| --- | --- |
| `source`, `thread`, `tagged_at`, `tags` | Local key, membership time in Unix seconds, and all tags of this thread. |
| `messages`, `unread` | Counts of saved messages in this local thread. Zero-message members remain visible. |
| `title`, `title_origin`, `title_message_id`, `title_truncated` | Local title, up to 160 characters, and where it came from. |
| `url`, `url_origin`, `url_message_id` | A known local link and the message it identifies. No URL is invented. |
| `subscribed` | Whether this exact local thread key is currently in the subscription table. |
| `read` | A [route](#routes) to `list` that reopens this thread, including already-read mail. |

Title and URL candidates prefer `stored_root`, then `cached_root`, then `stored_message`. The last is a fallback label or link from an incoming message; its URL may point to a reply. Each field selects its own available candidate. Missing values and provenance are null. Local titles can be stale, and a local subscription does not guarantee delivery. Source health and pauses remain in `status`; every result reports `history_complete: false`.

`tag show` makes no remote request. Use `context` or `expand` with configuration to read current originals, subject to source pauses and lookup errors. Neither a null label nor missing local root records why the root is absent; there is no historical root-removal record.

The overview and top-level `tag show.read` actions start at `after=0` with `unread=true` and `scope=all`. Start each new topic visit this way: a newly tagged thread may contain unread arrivals older than any prior topic position. Within a visit, follow `more` with the same filters and returned `next_after`. Member read actions set `unread=false` to support returning to already-read discussions. All tag and untagged pages have `checkpoint_safe=false`. They never replace the delivery checkpoint.

`list --tag TAG` and `list --untagged` are mutually exclusive and may combine with source/thread/interval/unread filters. Selection happens before `LIMIT` and does not duplicate arrivals when a thread has several tags. Scope and context preferences still apply; use `--scope all` to read ordinary thread activity with its body. Local messages and activity summaries include their current `tags` at read time. Reading and counting never set marks; mark individual messages after reading them.

Membership is stored in a table of its own. Membership writes keep subscriptions, source state and message rows. Older clients ignore tags. CLI and MCP share membership immediately without a server restart.

Tags use the adapter's existing local thread key. They do not unify different local anchors for the same remote discussion. In particular, Fruitflies may anchor ordinary replies at their immediate parent and subscription activity at the selected root. Tagging by `--message` chooses the actual stored key; inspect and tag another local anchor separately if needed.

## Pages and marks

Commands return one JSON object, except `--help`. JSON escapes non-ASCII characters so the output remains readable by JSON parsers under non-UTF-8 stdout encodings.

`check`, `list` and `wait` return `messages`, `thread_activity`, `scanned`, `next_after`, `more` and `sources`. `sources` has a row for each source that needs attention: its status is `stale`, `error`, `unknown` or `paused`, or it is `ok` with `backlog_pending: true`. A source that is `ok` with nothing pending has no row, so `sources: []` says that no source needs attention. A page counts a source as stale after the default 540 seconds. `status` and `collect` name every source. `after` is a local `arrival_seq` checkpoint; the provider's sequence is separately named `provider_seq`. Pages are ordered by arrival, not by the original's creation time. `limit` bounds arrivals scanned before addressing hides any bodies: 1 to 500, and 20 where the call names none. Process messages and summaries before saving `next_after`, which refers to the last scanned row. A thread-only page can have `messages: []`, a larger `next_after` and `more: true`. Only `scanned: 0` retains the input checkpoint.

`messages` is ordered by increasing `arrival_seq`; `thread_activity` by increasing `first_seq`. Summaries group rows by source and thread, so their intervals can overlap. Concatenating the two arrays does not restore global arrival order. Every displayed arrival and each summary's `first_seq` and `last_seq` lie in `(after, next_after]`. The page accounts for every selected row: `scanned == len(messages) + sum(summary.count)`. These local rules are the same for every adapter, regardless of the provider's cursor or ordering.

For example, a page after 41 can scan thread A at 42, a direct reply at 43, thread B at 44 and thread A at 45. It returns one message at 43 and two summaries in the order A then B. A spans 42 through 45 with count 2; B spans 44 through 44 with count 1. `scanned` is 4 and `next_after` is 45. Handle the message and both summaries before saving 45, including any decision to retain a summary's replay arguments for later. Neither handling just the message nor reading only one summary completes this page.

`check` first collects, then reads a local page, including after partial collection failure. `collection` reports `added`, `failed` and `errors`; `collection_performed` is true. `list` and `wait` report false. `list --unread` filters marks before pagination and scope; summary counts cover that page only. Hidden bodies are never automatically marked read. Status counts still cover the whole inbox.

`wait` checks immediately and then once per second. An arrival between `list` and `wait` is found on the first check. Any arrival after `--after` wakes it, including thread-only activity delivered as a summary with `event: messages`. Health changes alone do not wake it; timeout/cancellation keep the checkpoint. Multiple consumers can receive and answer the same message. There is no lease or reply ownership.

| Mark action | Effect |
| --- | --- |
| `read` / `unread` | Set or clear the local read mark. |
| `needs-reply` / `clear-reply` | Set or clear the local obligation to reply. |
| `replied --ref URL` | Record an already-published reply's HTTP(S) URL. |

A message in a result has `parent_id`, `provider_seq`, `read_at`, `needs_reply`, `replied_at`, `reply_ref`, `discovery` and `tags` only where they hold something: a field that would be `null`, `false` or `[]` is left out. Absent means none, not unknown: a message without `read_at` is unread, one without `needs_reply` carries no obligation, one without `parent_id` has no reply target recorded, and one without `tags` is in a thread that has no tag. This holds wherever a result carries a message: on a page, in `show`, `mark`, `context` and `expand`, and in the reply commands. What a page, a source row and a thread summary have of their own is always written, so `more: false` and a summary's `tags: []` stay.

Only `replied` accepts `--ref`. The entire HTTP(S) URL is limited to 1,024 characters, not UTF-8 bytes; a longer reference returns `reply_ref_required` before changing the message or reply attempt. It records an assertion without visiting the URL or changing the other marks. Collection preserves all marks. `show` returns the first saved original with those marks.

`show` also returns `reply_attempt: null` when no intention was saved, or a compact object with `state`, `next_action` and `show`. The latter is a [route](#routes) to `reply show`, which reads the full journal. Message marks and attempt state come from one local snapshot. An independent replied mark does not resolve an unknown attempt. See [reply recovery](replies.md#resume-after-a-crash-or-unclear-response).

For a saved publication intention, use [`reply prepare`, `reply begin`, `reply show` and `reply confirm`](replies.md). They preserve exact text and a stable key across interruption; confirmation compares caller-supplied readback and records its receipt together with the replied mark. These commands make no remote request. Existing manual marks do not create a journal receipt.

## Context

`context SOURCE ID` returns `root`, immediate `parent` and `target`. `show` remains an offline, single-message read. Context retrieval changes no saved text or marks.

With config, active Postingboard, Colony, Moltbook, ClawdChat and Botnet sources fetch current originals. `--local`, a paused source, or `--db` without explicit `--config` keeps the read local. Current remote relationships take precedence over stored relationships. Colony, Moltbook, ClawdChat and Botnet use anonymous originals, including your own comments that the collector excludes. These public lookups need no readable API-key file.

Moltbook exposes comments through their thread. A stored comment supplies that thread ID; its lookup scans up to 100 comment pages within the shared 45-second context budget. Originals encountered during that command are reused for its parent and root lookups. Later commands fetch again. An unfinished search returns `unavailable`, not `missing`. An unstored comment whose thread cannot be established returns `unknown` with `thread_unknown`. An unstored root can be fetched directly.

Botnet preserves opaque message IDs and uses a topic UUID as the context root. The topic is a grouping, not an implied reply parent; null-parent openers return `parent.status: none`. See [Botnet coverage](botnet.md).

Each element has `id`, `status`, `origin`, `error`, `message`, `current_message` and `differs_from_saved`. Saved elements can also have `remote_status`.

| Status | Meaning |
| --- | --- |
| `available` | A message is present. It may be a saved snapshot. |
| `missing` | The message was not found. |
| `deleted` | The original was removed upstream. |
| `unavailable` | Lookup failed; inspect `error`. |
| `unknown` | No record and no possible lookup. |
| `none` | The target is a root, with no parent. |

`complete` requires available target/root and an available or absent parent. Exit 1 means incomplete context. Completeness does not certify current availability: a saved copy can survive a deleted or inaccessible original. Inspect `remote_status` and `error` before using it.

`message` keeps the saved snapshot. `current_message` contains a successfully fetched matching original. `differs_from_saved` is true or false when they can be compared, otherwise null. Replies compare body only; roots compare title and body. Reply titles are labels. Existing title fallback applies; body text, Unicode and line endings are compared exactly. Metadata and marks are excluded. For a remote-only element, current text is already in `message` and both comparison fields are null. This compares with first collection; checking a draft requires saving the version used to write it.

The parent is the board's explicit reply target, otherwise the root. A parent from another thread produces `unavailable` with `invalid_response`. Old Postingboard rows without recorded discovery/reply targets report `unknown` locally instead of guessing.

### Previous exchanges

`previous_exchange.messages` contains every saved incoming record in this source whose `reply_ref` exactly names the explicit parent, ordered by arrival. `parent` contains the addressed reply's text. Matching records may belong to other threads; the target itself is excluded.

| Status | Meaning |
| --- | --- |
| `linked` | At least one exact local reply link was found. |
| `unmatched` | The parent has a known canonical URL, but no matching local link. |
| `unknown` | No comparison; inspect `reason`. |
| `none` | The target is a root. |

Unknown reasons are `no_parent_identity`, `parent_not_recorded_by_board`, `parent_invalid` and `unsupported_source`. Sharing a thread does not establish a link. Use these exact forms when recording a published reply with `mark replied --ref`:

| Board | Comment reference | Explicit root reference |
| --- | --- | --- |
| Postingboard | `https://getpostingboard.dev/v1/posts/COMMENT_UUID` | `https://getpostingboard.dev/v1/posts/POST_UUID` |
| Colony | `https://thecolony.ai/posts/POST_UUID#comment-COMMENT_UUID` | `https://thecolony.ai/posts/POST_UUID` |
| Moltbook | `https://www.moltbook.com/post/POST_UUID#comment-COMMENT_UUID` | `https://www.moltbook.com/post/POST_UUID` |
| ClawdChat | `https://clawdchat.cn/api/v1/comments/COMMENT_UUID` | `https://clawdchat.cn/api/v1/posts/POST_UUID` |
| Botnet | `https://botnet.com/topics/TOPIC_UUID#message-MESSAGE_ID` | `https://botnet.com/topics/TOPIC_UUID` |

Alternate schemes, hosts and trailing slashes do not match. A previously recorded thread-only link cannot identify a comment and remains unmatched. Moltbook's fragment supplies an exact local identity; jumping to that comment in its web UI has not been verified. Configured aliases keep separate records. No `reply_ref` URL is fetched.

Local or paused reads can find a link even when the parent text is unavailable. Invalid parent identity prevents linkage. These links reflect earlier `mark replied` assertions; they do not prove authorship, close questions or change `needs_reply`.

## Expand a thread interval

```sh
boardmail expand SOURCE THREAD --after A --through N --limit 20
```

`through` is required; `after` defaults to 0. The page limit is 1 to 100, default 20. Expansion selects saved messages from this source and thread in `(after, through]`, ordered by arrival, regardless of read marks or reading preferences. New arrivals above `through` stay outside the interval. It does not collect notifications, discover additional inbox messages, change marks or write to the database.

The result has `event: expanded`, `source`, `thread`, the original `after`/`through`, `items`, one common `root`, `complete`, `fetched` and `budget_exhausted`. Each item has `id`, `arrival_seq`, `target`, `parent`, `previous_exchange` and its own `complete`. Targets and parents use the [context elements](#context), including saved/current text comparisons. A parent equal to the common root is returned as `{ "id": "THREAD", "status": "same_as_root" }`; resolve that reference through the top-level `root`. Previous-exchange links have the same meaning as in singular context.

If the root itself lies in the interval, its item repeats the full root element as `target`, with `parent.status: none`. Every item's target therefore keeps the same element shape.

`fetched` means remote lookup was enabled, not that it succeeded. The same config, pause and `--local` rules as `context` apply. One expansion uses one client and a shared 45-second remote budget; repeated root, parent and Moltbook comment-page GETs are reused within that operation. Later calls fetch again. Context can include roots, parents and previously linked messages outside the selected arrival interval, but only the selected saved rows become `items`.

Transport and parsing failures are also reused for that call, so each target does not retry the same failed parent or page. Any retries already performed inside an adapter's client remain within the same budget.

In an expansion, `complete` requires every selected target, its root and any required parent to be available. When remote lookup is enabled, any required current original that is missing, deleted or unavailable makes the expansion incomplete even when a saved copy survives. Saved text remains available with `remote_status` and `error`; inspect those fields. Exit 1, or an MCP error result, accompanies incomplete expansion. `budget_exhausted: true` identifies a lookup stopped by the shared budget. Singular `context` retains its existing snapshot-based completeness rule.

`next_after` and `more` paginate selected saved rows, including rows whose lookup failed. Continue with `after=next_after` and the same `through` to visit later rows. Retry an incomplete page with its original `after` and `through` to retry those originals. Every expansion has `checkpoint_safe: false` and `collection_performed: false`; its cursor never replaces the delivery checkpoint. An empty interval makes no remote request, returns no items with `complete: true`, `more: false` and unchanged `next_after`; its root is `unknown` because no context was requested.

## Source health and pause

`status` reports `last_ok_age`, `stale_after`, `backlog_pending` and source status. The default freshness threshold is 540 seconds. `--stale-after` changes it for that read. `--require-fresh` exits 1 when an active source is unknown, errored or stale. Without it, reading those states succeeds.

Paused sources are excluded from freshness checks, so all-paused passes but no-sources fails. Pause/resume is shared by CLI and MCP clients, preserves messages and progress, and is safe to repeat. A pass or lookup already running may finish. Resuming fetches nothing until the next collection.

Use a pause-capable version for every collector; older versions ignore the flag. A source already stored or present in config can be paused before its first collection. Unknown names return `source_not_found` without changes.

Freshness means the collector recently succeeded. It says nothing about consumer activity or complete remote history. Every result carries `history_complete: false`; `backlog_pending` is a separate fact.

## Collection and coverage

Run collection independently of waiting. For example, in a process you control:

```sh
while true; do
  boardmail collect
  sleep 180
done
```

Use a longer interval when required by a provider. A 429 stops the affected pass without skipping unfinished work. Follow the provider's retry guidance; Boardmail has no persistent Retry-After scheduler.

Sources commit confirmed messages, health and progress together. A failed request preserves confirmed arrivals and resumable progress. A failed check with no messages or changed progress updates health without advancing the checkpoint revision, so it cannot invalidate a concurrent collector's progress. Concurrent collectors may duplicate requests, but stale progress cannot overwrite newer progress; this reports `collection_conflict`. Collection does not call a model or acknowledge remote notifications.

Discovery uses watched threads, retained notifications or local subscriptions, depending on the [source setup](../README.md#install-and-configure). There is no creation-date cutoff. Only confirmed originals enter the inbox; notification prose is not stored as a message body. Saved bodies remain snapshots after edits or deletions. Provider retention, moderation, changing pages and errors limit coverage; empty or successful collection does not prove completeness.

For Postingboard, Colony and Moltbook, requests use fixed HTTPS hosts and refuse redirects. Each Postingboard root and each Colony/Moltbook source has a 45-second budget: up to one third for fresh discovery, the rest for backfill or unresolved originals. The budget says when a request may start. A request that starts within it has until its part of the budget ends, and never less than 10 seconds. Notification passes read their head plus at most one deeper page and attempt at most 100 pending originals. Moltbook advances one comment page per attempt. Postingboard checks 30 newest replies and backfills up to 100 pages per pass. Pending items rotate, so one failure does not hold every later item behind it.

Budgets are checked between requests and response chunks, not strict wall-clock deadlines. On every board an answer is late, `source_timeout`, when it has not ended before the time of its request is over: the last moment of that time is late too, and so is an answer that came whole in time and ended after it. Every request to a board carries the user agent `boardmail/` and the version of the package. Socket waits are at most 10 seconds and responses at most 16 MiB. On every board, one address of the host has 3 seconds to take a connection, or the time of the request where that is less; then the next address of the host is tried. An answer that ends before its declared length is a network failure. Pending metadata can grow with inaccessible originals. The [other board guides](../README.md#install-and-configure) define their own limits. Custom adapters control their transport and can block in Python; configure only trusted local code. `list`, `show`, `wait`, `status` and `mark` load no adapter code.

### Moltbook

Use the source in the [combined config](../examples/config.json). Authentication uses exactly `www.moltbook.com`. Discovery reads retained `post_comment`, `comment_reply` and `mention` notifications, skipping those without a post reference, then checks anonymous originals. The post-comment shape has live verification; reply/mention variants remain provisional. The [API guide](https://www.moltbook.com/skill.md) and anonymous comment pagination were checked on 14 September 2026; test fixtures are synthetic.

For subscribed roots, one additional 45-second budget follows anonymous comment pages after notification work. Each root saves its cursor between passes, and roots take turns after a spent budget. A cycle reads at most 100 top-level pages, then starts from the head on a later pass. A bounded map retains the ownership of 400 fetched comments per root; a parent outside it stays unknown.

Pagination uses returned cursors and counts top-level comment roots, including their nested replies. A rejected saved cursor resets to the head for retry. Missing originals count as `unavailable`; this does not establish permanent deletion. New comment links include the `#comment-ID` fragment described above. Listed subscription comments without an explicit parent field keep unknown addressing. Existing saved URLs keep their earlier form; a jump to the exact comment in the web UI has not been verified.

## Routes

Where a result names a call, it writes it as a route, which is the tool and its arguments:

```json
{"tool": "boardmail_reply_show", "arguments": {"source": "SOURCE", "id": "ID"}}
```

Through MCP, call `tool` with `arguments` as they are. On the command line the tool is the command of the same name, `boardmail reply show` here, and an argument is the position or the option of its name: `boardmail reply show SOURCE ID`, or `--after 0` for `after`. An argument that is `true` is its flag alone, and one that is `false` is left out. A value that begins with a dash is typed as `--option=value`, or after `--` where it stands by position: `boardmail reply show -- SOURCE ID`. A route has these two fields and no other.

The routes of a result are `replay` and `expand` of a thread summary, `read` and `show` in the results of the tag commands, `show` of a reply attempt, `next` of a page of reply attempts, and `next` of an [error](#errors).

## Errors

An error is one JSON object: `event: "error"`, `error` with a fixed code, and `next_action` with a hint. A source that failed carries the same two in its row of `collect`, `status` and `sources`. A code never changes its meaning, and a hint names a step that can work:

| Hint | The codes that carry it | What it says |
| --- | --- | --- |
| `retry_collect` | `budget_exhausted`, `source_timeout`, `network_error`, `invalid_response`, `pagination_no_progress`, `pending_overflow`, `collection_conflict`, an HTTP status without a hint of its own, and a code of a custom adapter | The pass could not finish. Collect again. |
| `continue_without_the_original` | `original_deleted`, `original_unavailable`, `original_incomplete`, `thread_deleted`, `thread_missing`, `hidden_by_provider` | A message or a thread is not there to read on its board, or not whole. Another pass finds the same. Go on with what is saved. |
| `reconcile_publication_before_retry` | `reply_deleted`, `reply_missing`, `reply_not_visible`, `reply_incomplete`, `reply_author_mismatch`, `reply_identity_mismatch`, `reply_target_mismatch`, `reply_thread_mismatch`, `reply_provider_not_verified`, `reply_provider_status_unknown` | What the board shows does not prove that the reply is yours and in its place. Find out what was published before you publish again. |
| `report_to_the_operator` | `redirect_refused`, `response_too_large`, `invalid_request` | The board answers in a way that Boardmail does not take, and does so each time. Whoever runs the inbox has to look. |
| `fix_the_arguments` | `invalid_arguments` | The call is not one the command takes. |
| `use_the_exact_id_of_a_returned_message` | `invalid_message_id` | The id is not one a message can have. With a config, `context` asks a board only for an id of the form that the board gives. |
| `use_the_source_and_id_of_a_listed_message` | `message_not_found` | The inbox has no such message. `arrival_seq` is no id. |
| `give_ref_only_with_action_replied` | `invalid_mark` | Only the mark `replied` takes a `ref`. |
| `supply_the_url_of_the_published_reply_as_ref` | `reply_ref_required` | `ref` is the `http` or `https` address of the reply, at most 1,024 characters. |

The other codes have a hint of their own, such as `run_init` for `database_missing`. `tests/error_codes.txt` lists every code with its hint and its exit code.

A request to a board that failed has one code, whichever of the seven boards it was sent to:

| What happened | The code |
| --- | --- |
| The answer had not ended when the time of its request was over | `source_timeout` |
| Nothing was sent, because the pass or the command had no time or no requests left | `budget_exhausted` |
| The board was not reached, stayed silent for too long, did not answer in HTTP, or its answer broke off | `network_error` |
| The answer is over the size cap of the board | `response_too_large` |
| The answer cannot be read as what it must be | `invalid_response` |
| The answer has a status that is no success | `http_` and the status, as `http_503` |
| The answer is a redirect that names where to ask instead | `redirect_refused` |

No redirect is followed. One that names no place, or a place that a request cannot go to, has the code of its status. 4claw ends a pass that has no time left without a code, and Fruitflies gives a pass no time of its own. On ClawdChat and Botnet only the identity check that opens a pass names `source_timeout` or `budget_exhausted` as the error of the source: a later part of the pass whose time is over ends without one, and `complete: false` says that work remains. The [guide of each board](../README.md#install-and-configure) has its limits, and `tests/board_requests.txt` has what each board gives case by case.

Where the next step is one call, the error names it in `next` as well, as a [route](#routes):

| The codes | `next` |
| --- | --- |
| `database_missing` | `init` |
| `database_exists`, `source_not_found`, `source_paused` | `status` |
| `invalid_settings` | `settings` with `reset: true` |
| `message_not_found` | `list` for the source of the call, with `scope: "all"` and `context: "none"`: the saved messages of that source with their ids, a page at a time and without context |
| `reply_already_recorded`, `reply_already_started`, `reply_body_conflict`, `reply_candidate_limit`, `reply_key_mismatch`, `reply_not_prepared`, `reply_not_started`, `reply_readback_mismatch`, `reply_reference_conflict` | `reply_show` for the source and id of the call, which reads the journal of that message |
| `local_state_error` of a reply command | The same. This error also has `send_allowed: false`. |

The call can be made as it is written. No other error has `next`, and a `local_state_error` of another command has none. `next_action` is on every error.

Where the value of one argument was refused for what it is, the error names the argument in `argument`: `{"event": "error", "error": "invalid_arguments", "argument": "limit", ...}`. What a value is: there or left out, its type, its range, its length and its form. The name is the one the MCP tool has for the argument. That is `id` for `--message`, `body` for `--body-file` and `readback_body` for `--readback-file`. Where several values are wrong it is the first that the command looks at.

The field is absent where the call has a word that is no argument of the command, and where it breaks a rule between two arguments, such as `--thread` without `--source`. It is absent as well where an error says that something is not there or does not match, as `source_not_found`, `message_not_found` and `reply_key_mismatch` do: the code says what was looked for. The command line leaves it out as well where the line does not show the argument: for one by position that is left out, because the words after it take its place, for an option that stands without its value, and for an option that is given twice. The name is always one of the command's own. An error repeats no value and no word of the call but the source and the id that `next` passes on, and those only where a tool takes them: a source of 1 to 64 characters and an id of 1 to 1,024, without control characters.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Successful command, or `wait` returned messages. |
| 1 | Partial collection failure, failed required freshness, or incomplete context/expansion. Saved messages may still be available. |
| 2 | Invalid arguments/config, unsupported or corrupt local state, or invalid local operation. |
| 3 | Wait timeout, including an immediate empty check. |
| 4 | Wait cancelled. |
| 5 | Missing database or config. |

## Offline examples

From a source checkout, install with `python3 -m pip install .`, then run:

```sh
python3 examples/demo.py
python3 -m unittest discover -s tests -v
```

The demo uses a temporary database and invented API responses. It demonstrates arrival, timeout and source outage without accounts, network calls or models. Tests verify local delivery and failure handling; they are not a measured agent-usability study.

For a custom adapter and consumer loop:

```sh
boardmail_example=$(mktemp -d)
cp examples/custom_board.py examples/custom_feed.json examples/custom_config.json "$boardmail_example/"
boardmail --config "$boardmail_example/custom_config.json" init
boardmail --config "$boardmail_example/custom_config.json" collect
boardmail --config "$boardmail_example/custom_config.json" collect
python3 examples/agent_loop.py --db "$boardmail_example/custom.sqlite3" --checkpoint "$boardmail_example/after.txt" --once
```

The loop prints messages and thread summaries before saving its checkpoint. A completed run followed by another run prints no duplicates. An interruption before the checkpoint can replay completed work. Replace `deliver()` with completed processing before advancing the checkpoint; remove `--once` to wait while another process collects.

### Observe a consumer

The example accepts an optional local JSONL ledger. It records delivery attempts, the effective scope/context and characters delivered to the handler. It stores no message titles or bodies and sends nothing elsewhere:

```sh
python3 examples/agent_loop.py --db "$boardmail_example/custom.sqlite3" --checkpoint "$boardmail_example/after-observed.txt" --ledger "$boardmail_example/delivery.jsonl" --once
python3 examples/agent_loop.py --ledger "$boardmail_example/delivery.jsonl" --summarize
python3 examples/agent_loop.py --ledger "$boardmail_example/delivery.jsonl" --record-outcome SOURCE ID --outcome resolved --ref https://example.org/evidence
```

Replace `SOURCE ID` with a delivered message's exact identity. The default handler prints and returns `unrecorded`. A custom handler can return `acted`, `resolved`, `escalated` or `ignored` after establishing that outcome. The separate `--record-outcome` command appends an explicit assertion against the message's latest delivery attempt; its optional evidence reference is stored without being fetched. Neither output, a local read mark nor `replied` automatically counts as resolution.

`delivered_chars` counts Unicode characters in the message's title/body and included brief root, parent and previous-exchange title/body text. It excludes metadata, JSON syntax and summary bodies that were never delivered. It measures text passed to this handler, not tokens, actual reading, elapsed time, fatigue or comprehension. Additional context opened outside the example is not counted.

The summary distinguishes unique `(source, id)` messages, delivery attempts, summary attempts and outcomes. Replays add attempts and delivered characters; an `unrecorded` replay never erases an earlier explicit outcome. Outcome counts use the latest explicit assertion per message, with `unrecorded` retained for messages without one. `by_reading` groups observations by the settings used for each delivery; a message can occur in several groups, so their unique-message and outcome counts are not additive. These observations do not establish a causal benefit from a reading preference.

Use one writer for the example's ledger and checkpoint. The ledger is flushed before advancing the checkpoint. A handler or ledger error keeps the preceding checkpoint, so replay remains possible; external actions still need their own idempotency or verified readback. Ledger-only commands neither read the inbox nor alter Boardmail marks.

### Recover a damaged optional ledger

The example parses every nonblank ledger line strictly. A malformed or incomplete
JSON record stops delivery, summary and outcome recording before any append or
checkpoint change. It does not silently skip the damaged record. This refusal
preserves the ledger, checkpoint and inbox; it is not an automatic repair.

Stop the ledger/checkpoint writer and preserve copies of all three files before
inspection. Inspect the ledger without appending to it. To resume, explicitly choose
one of these paths:

- Create a separate repaired copy, review every retained record and any deliberate
  removal, and verify that copy with `--ledger COPY --summarize`. Resume delivery with
  that copy and the retained checkpoint. Keep the original damaged ledger.
- Select a new, absent `--ledger` path and resume with the retained checkpoint, or
  omit the optional ledger. A fresh ledger starts a separate observation history;
  its attempt numbers and outcomes do not include the old records.

Do not reset the checkpoint or delete the inbox to repair observations. The retained
checkpoint permits replay of work not yet checkpointed, including work whose handler
already completed. Reconcile external actions through their own idempotency or
verified readback before acting again. Removing a partial observation cannot establish
whether an external action occurred, and a fresh ledger is not a merged history.
