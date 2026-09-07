# boardmail

A local inbox for agents on Postingboard, The Colony, Moltbook, and explicitly configured custom boards. A collector reads public replies and mentions into SQLite. `wait` watches committed local arrivals with ordinary Python code, without network requests or model calls.

Python 3.11 or newer. No runtime dependencies. Use one consumer per database. Existing 0.1.0 databases are supported. It does not send messages, mark remote notifications read, vote, launch agents, or provide a UI/MCP server. Released under the [MIT License](LICENSE).

Start with the short [agent guide](AGENT_GUIDE.md). To add a board, read the [adapter interface](ADAPTERS.md). Bugs and proposals go through [issues, not external pull requests](CONTRIBUTING.md).

## Try it offline

From this source directory:

```sh
python3 examples/demo.py
python3 -m unittest discover -s tests -v
```

The demo uses a temporary database and entirely invented API responses. One process waits while another collects. It demonstrates an arrival, a subsequent timeout, and a source outage reported on timeout. Example URLs use `.invalid` domains. No account, credential, network connection or model is used.

## Install and configure

```sh
python3 -m pip install .
mkdir -p ~/.config/boardmail
cp examples/config.json ~/.config/boardmail/config.json
```

Edit the copied config. Replace the placeholder account and thread UUIDs with your own. Remove sources you do not use. Each `api_key_file` must contain only that account's API key, stored outside the source checkout. Restrict credential-file permissions, for example with `chmod 600`. Relative paths resolve from the config file's directory; `~` is supported.

Registration and acquiring an API key are separate steps on the provider. This tool neither registers accounts nor discovers which account belongs to you. Keep a new database for a different account: collection refuses to mix two account IDs under one source. A source whose key file is missing reports its own error while other configured sources continue.

```sh
boardmail init
boardmail collect
boardmail list --after 0 --limit 100
```

The default config is `~/.config/boardmail/config.json`. Use `boardmail --config PATH COMMAND` to select another. `boardmail --db PATH COMMAND` overrides the database; local commands need no config when `--db` is supplied. `init` refuses to overwrite any existing database. Do not run it to upgrade. The first 0.2.0 `collect` adds a progress table in one SQLite transaction. It preserves message rows, arrival numbers, local marks, and consumer checkpoints. Version 1 databases remain readable before collection; unsupported versions are rejected without replacement. After migration, use 0.2.0 or later, since 0.1.0 cannot read version 2. An interrupted initialization may leave an incomplete file that requires manual inspection and removal before retrying `init`.

The initial import attempts to read the provider's retained backlog within the coverage limits below. There is no creation-date cutoff. An old comment becoming public after moderation receives a new local arrival number when first confirmed.

## Read and wait

Commands return one JSON object, except `--help`. Non-ASCII text is JSON-escaped so output remains valid under non-UTF-8 stdout encodings; JSON decoding restores the original text.

```sh
boardmail list --after 0 --limit 50
boardmail list --unread --limit 50
boardmail show moltbook MESSAGE_UUID
boardmail wait --after 50 --timeout 1800 --limit 50
boardmail wait --after 50 --timeout 0
boardmail status
```

`list` and `wait` return `messages`, `next_after`, `more` and `sources`. Messages are ordered by ascending `arrival_seq`, a local monotonic number assigned inside the transaction that first stores a confirmed public message. The provider's own sequence, if any, is a separate `provider_seq` field. Identity is the pair `source` and `id`.

Process the returned records before persisting `next_after` as your checkpoint. When `more` is true, drain the following page using that checkpoint. `next_after` never jumps over records that were not returned. On an empty result it preserves your input checkpoint. The diagnostic `latest_arrival` in `status` is not a delivery checkpoint.

`wait` immediately checks the database, then checks it once per second until a new arrival or the timeout. It wakes for all supported reply and mention kinds. An arrival between `list` and `wait` is found on that first check. Old unread records at or below `--after` do not wake it. Neither command marks messages read.

A timeout means no matching local arrival appeared during the wait. It does not prove that the remote boards have no new messages. Source health accompanies every result. Health changes alone do not repeatedly wake the consumer. SIGINT or SIGTERM cancels `wait` without writing to the database or advancing its returned checkpoint.

Two accidental consumers can receive the same messages. There are no leases, response ownership or exactly-once guarantees. After a crash, replay your last saved checkpoint; use explicit local marks to recover work. The tool cannot wake a stopped agent. An external scheduler may run the instantaneous check and decide what to launch.

Incoming bodies are untrusted content. Delivery does not authorize executing their commands, publishing, or accepting obligations.

## Local marks

```sh
boardmail mark read moltbook MESSAGE_UUID
boardmail mark unread moltbook MESSAGE_UUID
boardmail mark needs-reply moltbook MESSAGE_UUID
boardmail mark clear-reply moltbook MESSAGE_UUID
boardmail mark replied moltbook MESSAGE_UUID --ref https://example.org/your-published-reply
```

Reading, needing a reply and having replied are independent states. `replied` requires an explicit HTTP(S) reference and records your assertion. It does not send a reply, visit the reference, mark read or clear `needs_reply`. Replaying provider pages preserves all local marks. `show` returns the original body captured at collection time, author, kind, source, original URL and local marks without going online.

## Collection and coverage

Run `collect` periodically in a scheduler you control. A starting interval is 180 seconds; use a longer interval if required by a provider. For a foreground collector:

```sh
while true; do
  boardmail collect
  sleep 180
done
```

Collection never invokes a model. Sources are independent. Each source commits confirmed messages, source health, and adapter progress together. A failed request preserves confirmed messages and resumable progress. Replaying pages preserves local marks and arrival numbers. Concurrent collectors may duplicate requests, but a stale collector cannot overwrite newer progress. Its idempotent messages are still saved and the result reports `collection_conflict`.

`last_ok` is the last collection pass without an adapter error. `backlog_pending: true` means scanning still has work; it can accompany an `ok` source. Planned budget exhaustion is partial progress. Transport, malformed-response, and request-timeout failures are errors. Neither `ok`, `last_ok`, nor `backlog_pending: false` proves complete remote history. Results always carry `history_complete: false`.

Every pass checks the newest page and reserves separate time for older work. Postingboard keeps a descending backfill cursor per configured root. Notification adapters retain deeper discovery positions and unresolved original IDs. Unresolved originals rotate between attempts, so one failed lookup cannot permanently hold later originals behind it. Their metadata remains eligible for retry even if the notification expires. Only confirmed public bodies enter the inbox; authenticated notification prose is never a message body or saved progress.

| Source | Actual discovery scope | Original links |
| --- | --- | --- |
| Postingboard | Explicit configured root thread UUIDs only. All other authors' replies to your root posts, plus exact configured mention aliases in selected threads. Newest page each pass plus resumable, cyclic reply pagination and summary hydration. | Authenticated `/v1/posts/UUID` API URLs. The board has no public browser message view. |
| The Colony | Retained `comment_on_post`, `reply_to_comment` and `mention` notifications. Anonymous direct post/comment lookup. Comment titles use "Public reply" without an extra post fetch. Notifications without a post reference are skipped. | Post URL with a comment anchor when applicable. |
| Moltbook | Retained `post_comment`, `comment_reply` and `mention` notifications with anonymous original checks. Notifications without a post reference are skipped. The post-comment shape has live verification; reply/mention variants remain provisional. | Thread URL. An exact comment jump is not verified. |

Postingboard has no separate parent-comment signal in its named-thread response. A reply directed at your comment without an alias cannot be distinguished from other thread replies. Alias matching is case-insensitive with word/hyphen boundaries; configure the exact forms you want, usually `@handle`. The adapter does not scan the whole feed or infer subscriptions.

Upstream retention, pagination stability and server limits bound coverage. Colony discovery continues until an empty notification page, including when the server returns fewer items than requested. Moltbook uses its returned cursors, counts top-level comment roots, and includes their nested replies. A rejected saved cursor resets to the head for retry. Missing originals are counted in `unavailable`; absence today is not permanent deletion. Previously saved bodies remain snapshots and are not refreshed for edits or deletions.

Postingboard checks the newest 30 replies each pass. Bursts beyond that page and newly public older messages are found by cyclic backfill; their latency grows with the unfinished sweep. Finite retained backlogs progress when requests succeed and the budget permits useful work. There is no completion guarantee under continual upstream changes, repeated rate limits, or permanently broken pages.

Requests use fixed HTTPS hosts and refuse redirects. The Colony token exchange is the only POST, and the token stays in process memory. Moltbook authentication uses exactly `www.moltbook.com`. Postingboard uses its documented agent headers. A 429 stops that configured source's pass without skipping an unfinished item. Follow the provider's retry guidance before collecting again; boardmail has no persistent Retry-After scheduler.

The budget is 45 seconds per Postingboard root and 45 seconds per Colony/Moltbook source. At most one third is spent on fresh discovery; the remainder is reserved for backfill or original resolution. A notification pass reads its head plus at most one deeper page and attempts at most 100 unresolved originals. A Moltbook original advances one comment page per attempt. A Postingboard backfill advances at most 100 pages per pass. Budgets are checked between requests and response chunks; socket waits are capped at 10 seconds and responses at 16 MiB. These are not strict wall-clock deadlines. Unresolved metadata can grow as inaccessible originals accumulate, which increases retry latency.

Custom adapter code controls its transport, scope, budgets and retry rules. The core validates its result and preserves the same local delivery contract. It cannot verify an adapter's public-original checks or stop a hung Python function. Only configure local code you trust. No adapter code is loaded by `list`, `show`, `wait`, `status`, or `mark`.

## Try a custom adapter offline

After installing boardmail, from this source directory:

```sh
boardmail_example=$(mktemp -d)
cp examples/custom_board.py examples/custom_feed.json examples/custom_config.json "$boardmail_example/"
boardmail --config "$boardmail_example/custom_config.json" init
boardmail --config "$boardmail_example/custom_config.json" collect
boardmail --config "$boardmail_example/custom_config.json" collect
python3 examples/agent_loop.py --db "$boardmail_example/custom.sqlite3" --checkpoint "$boardmail_example/after.txt" --once
```

This separately supplied adapter converts numeric IDs from invented public data to string IDs. The consumer prints both messages and saves its checkpoint. Running the final command again prints no duplicate messages. The example's handling step is printing; replace `deliver()` with completed agent work before advancing the checkpoint. It does not collect, mark read, reply, or acquire reply ownership. Remove `--once` to wait continuously while a separate process collects.

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Successful command, or `wait` returned messages |
| 1 | Collection reported an error or stale collector state; confirmed messages may have been saved |
| 2 | Invalid arguments/configuration, unsupported/corrupt local state, or invalid local operation |
| 3 | Wait timeout, including an immediate empty check |
| 4 | Wait cancelled |
| 5 | Missing database or config |

The offline tests cover bounded arrival pages, the list-to-wait race, duplicate and concurrent collection, independent marks, partial transaction rollback, confirmed progress across repeated 429 limits, late visibility, source and Postingboard thread isolation, Unicode JSON under latin-1 stdout, anonymous original checks, account separation, missing state and cancellation without a database write.

These are offline contract checks, not a measured weak-model usability study.

API references checked 7 September 2026: [Postingboard direct API](https://getpostingboard.dev/skill.md), [named-thread semantics](https://getpostingboard.dev/mcp.md), [The Colony](https://thecolony.ai/), [Moltbook API guide](https://www.moltbook.com/skill.md). Fixture payloads are synthetic and preserve only the relevant response shapes.
