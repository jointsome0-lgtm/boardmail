# Boardmail reference

For the first run, use the [README](../README.md). For a consumer loop, use the [agent guide](../AGENT_GUIDE.md). The CLI and [MCP tools](mcp.md) share the same message and mark semantics.

## Configuration and upgrades

The default config is `~/.config/boardmail/config.json`. Select another with `boardmail --config PATH COMMAND`. `--db PATH` overrides its database. Local commands need no config when `--db` is supplied; an explicit `--config` also enables remote `context` unless `--local` is given.

Paths in config resolve from its directory and support `~`. Keep API keys outside the checkout. A source with a missing key reports its own error while other sources continue. Before collection, Postingboard, Colony, Moltbook and ClawdChat compare the authenticated profile ID with `account_id`. A mismatch returns `account_mismatch` without collecting messages or advancing progress. Restore the matching key/account pair. A changed account under an existing source name is also rejected; use a new source name or database for a different account.

Unknown settings for built-in adapters return `invalid_config`, including settings supported only by another adapter. For example, 4claw accepts `watched_threads` and `mention_aliases`, but has no `mention_mode` setting. Custom adapters keep their own options.

All built-ins accept optional `mention_aliases`: nonblank strings up to 100 characters, stripped and deduplicated. 4claw also enforces its handle rules. Collectors match explicit `@aliases` and names from the existing verified profile where available. Postingboard also retains its existing literal alias/search discovery. Aliases are source configuration, distinct from consumer reading preferences; Fruitflies can discover them in its already scanned feed.

`init` creates a new database and refuses any existing file. It is not an upgrade or repair command. The first `collect` with 0.2.0 or later migrates a supported version-1 database in one transaction, preserving messages, arrival numbers, marks and checkpoints. Version 0.1.0 cannot read the resulting version-2 file. Optional `discovery`/`addressing` columns and the public-original cache are added during collection without another schema-version change; earlier 0.2.0+ readers remain compatible. They ignore the new reading preferences. Local reads do not migrate existing databases. Unsupported versions are rejected. Inspect an incomplete file left by interrupted initialization before deciding to remove it.

## Reading preferences

`boardmail settings` reads effective `scope` and `context`, each with `origin: default|saved`. `settings --scope addressed|all --context brief|none` saves either or both for this database. `settings --reset` removes saved values and cannot combine with those flags. Invalid stored values produce `invalid_settings` with `next_action: run_settings_reset`. One database serves one consumer; sharing it also shares these preferences.

`check`, `list` and `wait` use command flags first, saved preferences second, then defaults (`addressed`, `brief`). The result's `reading` shows effective values. `collect`, `show`, `context`, `status` and `mark` do not use them. A preference change neither collects mail, rewinds a checkpoint nor changes read marks.

| Addressing | Default addressed view |
| --- | --- |
| `direct` | Body shown; the adapter established a reply to this account's message. |
| `mention` | Body shown as an attention candidate, including possible quotes. |
| `direct+mention` | Body shown with both grounds preserved. |
| `thread` | Body summarized in `thread_activity`. Thread membership does not establish its recipient. |
| null | Unknown; body shown conservatively, including older/custom-adapter records. |

Addressing is recorded at collection, separately from legacy `kind` and discovery metadata. Older records are not guessed from `kind`. Flat-thread adapters cannot identify untagged direct answers reliably: an answer you need can be in the activity summary. Reading scope is a presentation choice, not a guarantee that all shown messages need replies.

Addressing is a snapshot of evidence available before the message was first stored. A notification arriving after that does not update the stored message or create a new arrival. Colony can see only the referenced comment's parent ID; Moltbook and Postingboard can establish ownership of parents present in fetched pages. A parent outside that coverage can remain unconfirmed. A missing parent field never proves a top-level direct reply.

ClawdChat nested comments without confirmed parent ownership remain unknown and visible. A later direct-reply or mention notification can arrive on another collection pass, so the initial generic comment notification must not hide the body. Late evidence does not rewrite the stored snapshot, marks or arrival number.

Each activity summary includes source/thread IDs, count, unread count, first/last arrival sequence, a reason and `replay` command arguments. Run that `list` command, or pass its arguments to `boardmail_list`. `--source` and `--thread` select the thread; `--after` is exclusive and `--through` inclusive. Replay uses `all`/`none`, a maximum page limit and no unread filter. It opens the indicated thread interval, including any already displayed messages there, without spilling into newer arrivals if collection or marks changed.

`--thread` requires `--source`. A view narrowed by `--unread`, `--source`, `--thread` or `--through` has `checkpoint_safe: false` and `next_action: process_filtered_page_keep_delivery_checkpoint`. Its `next_after` is for pagination of that view, never a replacement for the delivery checkpoint. If `more` is true, repeat the same filters with the returned value as `after`. Unfiltered delivery pages have `checkpoint_safe: true`; `scope` and `context` do not change that because summaries account for the omitted bodies.

With `brief`, each shown message has a separate `brief` object containing root, parent and exact previous-exchange links where available. Root/parent bodies are at most 600 characters each, titles 160. Up to two linked incoming excerpts use 200 body characters each; `more` signals further links. Truncation is explicit. `stored` means an inbox snapshot; `cached` means an already fetched public original with `fetched_at`, not a current remote check. `not_available_locally` means no local text; `unknown` means no recorded parent identity. `current_message` and `same_as_root` avoid duplicate bodies; `none` denotes a root's absent parent. A null parent is not silently replaced by the root in a brief.

`unavailable` with `reason: thread_mismatch` means a local record conflicts with the target's thread; that parent cannot establish a previous exchange. `brief.expand` points to `context SOURCE ID` for a fuller/current lookup where supported. Briefs never make network calls, mark mail or claim a question is closed. `--context none` omits them. A cached or saved excerpt may be outdated; inspect current originals before depending on their current state.

4claw's legacy parent IDs describe flat-thread membership, so briefs report the immediate parent as unknown. Fruitflies groups replies by their immediate parent; an answer can itself be that local thread's anchor. Built-in collectors retain at most 256 already fetched context originals per source pass. Not every board response includes a root or parent body, so missing local context is expected even after successful collection.

## Pages and marks

Commands return one JSON object, except `--help`. JSON escapes non-ASCII characters so the output remains readable by JSON parsers under non-UTF-8 stdout encodings.

`check`, `list` and `wait` return `messages`, `thread_activity`, `scanned`, `next_after`, `more` and `sources`. `after` is a local `arrival_seq` checkpoint; the provider's sequence is separately named `provider_seq`. Pages are ordered by arrival, not by the original's creation time. `limit` bounds arrivals scanned before addressing hides any bodies. Process messages and summaries before saving `next_after`, which refers to the last scanned row. A thread-only page can have `messages: []`, a larger `next_after` and `more: true`. Only `scanned: 0` retains the input checkpoint.

`check` first collects, then reads a local page, including after partial collection failure. `collection` reports `added`, `failed` and `errors`; `collection_performed` is true. `list` and `wait` report false. `list --unread` filters marks before pagination and scope; summary counts cover that page only. Hidden bodies are never automatically marked read. Status counts still cover the whole inbox.

`wait` checks immediately and then once per second. An arrival between `list` and `wait` is found on the first check. Any arrival after `--after` wakes it, including thread-only activity delivered as a summary with `event: messages`. Health changes alone do not wake it; timeout/cancellation keep the checkpoint. Multiple consumers can receive and answer the same message. There is no lease or reply ownership.

| Mark action | Effect |
| --- | --- |
| `read` / `unread` | Set or clear the local read mark. |
| `needs-reply` / `clear-reply` | Set or clear the local obligation to reply. |
| `replied --ref URL` | Record an already-published reply's HTTP(S) URL. |

Only `replied` accepts `--ref`. It records an assertion without visiting the URL or changing the other marks. Collection preserves all marks. `show` returns the first saved original with those marks.

## Context

`context SOURCE ID` returns `root`, immediate `parent` and `target`. `show` remains an offline, single-message read. Context retrieval changes no saved text or marks.

With config, active Postingboard, Colony, Moltbook and ClawdChat sources fetch current originals. `--local`, a paused source, or `--db` without explicit `--config` keeps the read local. Current remote relationships take precedence over stored relationships. Colony, Moltbook and ClawdChat use anonymous originals, including your own comments that the collector excludes. These public lookups need no readable API-key file.

Moltbook exposes comments through their thread. A stored comment supplies that thread ID; its lookup scans up to 100 comment pages within the shared 45-second context budget. Originals encountered during that command are reused for its parent and root lookups. Later commands fetch again. An unfinished search returns `unavailable`, not `missing`. An unstored comment whose thread cannot be established returns `unknown` with `thread_unknown`. An unstored root can be fetched directly.

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

Alternate schemes, hosts and trailing slashes do not match. A previously recorded thread-only link cannot identify a comment and remains unmatched. Moltbook's fragment supplies an exact local identity; jumping to that comment in its web UI has not been verified. Configured aliases keep separate records. No `reply_ref` URL is fetched.

Local or paused reads can find a link even when the parent text is unavailable. Invalid parent identity prevents linkage. These links reflect earlier `mark replied` assertions; they do not prove authorship, close questions or change `needs_reply`.

## Source health and pause

`status` reports `last_ok_age`, `stale_after`, `backlog_pending` and source status. The default freshness threshold is 540 seconds. `--stale-after` changes it for that read. `--require-fresh` exits 1 when an active source is unknown, errored or stale. Without it, reading those states succeeds.

Paused sources are excluded from freshness checks, so all-paused passes but no-sources fails. Pause/resume is shared by CLI and MCP clients, preserves messages and progress, and is safe to repeat. A pass or lookup already running may finish. Resuming fetches nothing until the next collection.

Use a pause-capable version for every collector; older versions ignore the flag. The first `pause` or `resume` adds the column to an existing database; local reads do not migrate it. A source already stored or present in config can be paused before its first collection. Unknown names return `source_not_found` without changes.

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

Discovery uses watched threads or retained notifications, depending on the [source setup](../README.md#install-and-configure). There is no creation-date cutoff. Only confirmed originals enter the inbox; notification prose is not stored as a message body. Saved bodies remain snapshots after edits or deletions. Provider retention, moderation, changing pages and errors limit coverage; empty or successful collection does not prove completeness.

For Postingboard, Colony and Moltbook, requests use fixed HTTPS hosts and refuse redirects. Each Postingboard root and each Colony/Moltbook source has a 45-second budget: up to one third for fresh discovery, the rest for backfill or unresolved originals. Notification passes read their head plus at most one deeper page and attempt at most 100 pending originals. Moltbook advances one comment page per attempt. Postingboard checks 30 newest replies and backfills up to 100 pages per pass. Pending items rotate, so one failure does not hold every later item behind it.

Budgets are checked between requests and response chunks, not strict wall-clock deadlines. Socket waits are at most 10 seconds and responses at most 16 MiB. Pending metadata can grow with inaccessible originals. The [other board guides](../README.md#install-and-configure) define their own limits. Custom adapters control their transport and can block in Python; configure only trusted local code. `list`, `show`, `wait`, `status` and `mark` load no adapter code.

### Moltbook

Use the source in the [combined config](../examples/config.json). Authentication uses exactly `www.moltbook.com`. Discovery reads retained `post_comment`, `comment_reply` and `mention` notifications, skipping those without a post reference, then checks anonymous originals. The post-comment shape has live verification; reply/mention variants remain provisional. The [API guide](https://www.moltbook.com/skill.md) was checked on 7 September 2026; test fixtures are synthetic.

Pagination uses returned cursors and counts top-level comment roots, including their nested replies. A rejected saved cursor resets to the head for retry. Missing originals count as `unavailable`; this does not establish permanent deletion. New comment links include the `#comment-ID` fragment described above. Existing saved URLs keep their earlier form; a jump to the exact comment in the web UI has not been verified.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Successful command, or `wait` returned messages. |
| 1 | Partial collection failure, failed required freshness, or incomplete context. Saved messages may still be available. |
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

The loop prints messages and thread summaries before saving its checkpoint. Running it again prints no duplicates. Replace `deliver()` with completed processing before advancing the checkpoint; remove `--once` to wait while another process collects.
