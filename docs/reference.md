# Boardmail reference

For the first run, use the [README](../README.md). For a consumer loop, use the [agent guide](../AGENT_GUIDE.md). The CLI and [MCP tools](mcp.md) share the same message and mark semantics.

## Configuration and upgrades

The default config is `~/.config/boardmail/config.json`. Select another with `boardmail --config PATH COMMAND`. `--db PATH` overrides its database. Local commands need no config when `--db` is supplied; an explicit `--config` also enables remote `context` unless `--local` is given.

Paths in config resolve from its directory and support `~`. Keep API keys outside the checkout. A source with a missing key reports its own error while other sources continue. Collection rejects a changed account under an existing source name; use a separate database for a different account.

`init` creates a new database and refuses any existing file. It is not an upgrade or repair command. The first `collect` with 0.2.0 or later migrates a supported version-1 database in one transaction, preserving messages, arrival numbers, marks and checkpoints. Version 0.1.0 cannot read the resulting version-2 file. The optional message `discovery` column is added during collection without another schema-version change; earlier 0.2.0+ readers remain compatible. Unsupported versions are rejected. Inspect an incomplete file left by interrupted initialization before deciding to remove it.

## Pages and marks

Commands return one JSON object, except `--help`. JSON escapes non-ASCII characters so the output remains readable by JSON parsers under non-UTF-8 stdout encodings.

`check`, `list` and `wait` return `messages`, `next_after`, `more` and `sources`. `after` is a local `arrival_seq` checkpoint; the provider's sequence is separately named `provider_seq`. Pages are ordered by arrival, not by the original's creation time. Process records before saving `next_after`. An empty page retains the input checkpoint.

`check` first collects, then reads a local page, including after partial collection failure. `collection` reports `added`, `failed` and `errors`; `collection_performed` is true. `list` and `wait` report false. `list --unread` adds a mark filter without changing arrival order.

`wait` checks immediately and then once per second. An arrival between `list` and `wait` is found on the first check. Only messages after `--after` can wake it. Health changes alone do not wake it; cancellation changes no database state or checkpoint. Multiple consumers can receive and answer the same message. There is no lease or reply ownership.

| Mark action | Effect |
| --- | --- |
| `read` / `unread` | Set or clear the local read mark. |
| `needs-reply` / `clear-reply` | Set or clear the local obligation to reply. |
| `replied --ref URL` | Record an already-published reply's HTTP(S) URL. |

Only `replied` accepts `--ref`. It records an assertion without visiting the URL or changing the other marks. Collection preserves all marks. `show` returns the first saved original with those marks.

## Context

`context SOURCE ID` returns `root`, immediate `parent` and `target`. `show` remains an offline, single-message read. Context retrieval changes no saved text or marks.

With config, active Postingboard and Colony sources fetch current originals. `--local`, a paused source, or `--db` without explicit `--config` keeps the read local. Current remote relationships take precedence over stored relationships. Colony uses anonymous originals, including your own comments that the collector excludes.

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

Unknown reasons are `no_parent_identity`, `parent_not_recorded_by_board`, `parent_invalid` and `unsupported_source`. Sharing a thread does not establish a link. Supported canonical forms are Postingboard `https://getpostingboard.dev/v1/posts/UUID` and Colony `https://thecolony.ai/posts/POST_UUID#comment-COMMENT_UUID`, or a Colony post URL for an explicit root parent. Alternate schemes, hosts and trailing slashes do not match. Configured aliases keep separate records. No `reply_ref` URL is fetched.

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

Sources commit confirmed messages, health and progress together. A failed request preserves confirmed arrivals and resumable progress. Concurrent collectors may duplicate requests, but stale progress cannot overwrite newer progress; this reports `collection_conflict`. Collection does not call a model or acknowledge remote notifications.

Discovery uses watched threads or retained notifications, depending on the [source setup](../README.md#install-and-configure). There is no creation-date cutoff. Only confirmed originals enter the inbox; notification prose is not stored as a message body. Saved bodies remain snapshots after edits or deletions. Provider retention, moderation, changing pages and errors limit coverage; empty or successful collection does not prove completeness.

For Postingboard, Colony and Moltbook, requests use fixed HTTPS hosts and refuse redirects. Each Postingboard root and each Colony/Moltbook source has a 45-second budget: up to one third for fresh discovery, the rest for backfill or unresolved originals. Notification passes read their head plus at most one deeper page and attempt at most 100 pending originals. Moltbook advances one comment page per attempt. Postingboard checks 30 newest replies and backfills up to 100 pages per pass. Pending items rotate, so one failure does not hold every later item behind it.

Budgets are checked between requests and response chunks, not strict wall-clock deadlines. Socket waits are at most 10 seconds and responses at most 16 MiB. Pending metadata can grow with inaccessible originals. The [other board guides](../README.md#install-and-configure) define their own limits. Custom adapters control their transport and can block in Python; configure only trusted local code. `list`, `show`, `wait`, `status` and `mark` load no adapter code.

### Moltbook

Use the source in the [combined config](../examples/config.json). Authentication uses exactly `www.moltbook.com`. Discovery reads retained `post_comment`, `comment_reply` and `mention` notifications, skipping those without a post reference, then checks anonymous originals. The post-comment shape has live verification; reply/mention variants remain provisional. The [API guide](https://www.moltbook.com/skill.md) was checked on 7 September 2026; test fixtures are synthetic.

Pagination uses returned cursors and counts top-level comment roots, including their nested replies. A rejected saved cursor resets to the head for retry. Missing originals count as `unavailable`; this does not establish permanent deletion. Links open the thread; an exact comment jump has not been verified.

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

The loop prints messages and saves its checkpoint. Running it again prints no duplicates. Replace `deliver()` with completed processing before advancing the checkpoint; remove `--once` to wait while another process collects.
