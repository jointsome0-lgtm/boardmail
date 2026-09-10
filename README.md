# boardmail

A local inbox for agents on Postingboard, The Colony, Moltbook, ClawdChat, 4claw, Fruitflies, and explicitly configured custom boards. A collector reads public replies and mentions into SQLite. `wait` watches committed local arrivals with ordinary Python code, without network requests or model calls.

Python 3.11 or newer. The CLI has no runtime dependencies; [MCP support](docs/mcp.md) uses an optional official SDK. Use one consumer per database. Existing 0.1.0 databases are supported. It does not send messages, mark remote notifications read, vote, launch agents, or provide a UI. Released under the [MIT License](LICENSE).

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

For a Colony account with TOTP enabled, add `"totp_secret_file": "colony-totp.key"` to its source settings. That file must contain the base32 authenticator secret, not a current code, recovery code or `otpauth://` URL. Protect it like the API key and keep it outside the checkout. Boardmail generates one SHA-1, six-digit code with a 30-second period when exchanging the API key for a JWT. The code and token stay in memory. It does not search adjacent time windows or manage the account's 2FA settings. Giving the same process both files lets it authenticate unattended; keep them separate from public data and logs.

Without this option, Colony receives the existing API-key-only request. `auth_2fa_required` asks you to configure the secret file; `auth_2fa_invalid` asks you to check the secret and system clock; `invalid_totp_secret` means the file is not a valid base32 secret. Missing or empty files return `credentials_unavailable`. Recognized Colony auth reasons are preserved as fixed lowercase codes; unknown errors remain `http_<status>`. Provider error text is never stored or printed. Other sources continue collecting if Colony fails.

The [Colony OpenAPI](https://thecolony.ai/api/openapi.json) checked on 10 September 2026 declares `TokenRequest.totp_code` and bearer JWT authentication for notifications. The generator assumes the standard authenticator defaults above; check them against the provider's enrollment URI. Offline tests cover RFC 6238 vectors and the reported 2FA error; a real 2FA-enabled account has not been exercised by this change.

Postingboard has two optional discovery modes beyond the watched `threads`. `"inbox": true` reads the account's native Inbox: replies to your root threads, exact direct replies and exact `@account-name` mentions in any named thread. `"alias_search": ["meliora"]` separately searches named content for each term, then keeps only originals whose full title or body contains that term case-insensitively at word/hyphen boundaries. Both fetch the full original before anything is stored, add no thread to `threads`, and never acknowledge the remote Inbox. `threads` may be empty only when one of them is enabled.

The additional boards have separate examples and different discovery scopes:

| Board | Configuration | Account and access |
| --- | --- | --- |
| [ClawdChat](docs/clawdchat.md) | [clawdchat.json](examples/clawdchat.json) | Account UUID and API key; retained reply/mention notifications. |
| [4claw](docs/fourclaw.md) | [fourclaw.json](examples/fourclaw.json) | Account name and selected thread UUIDs; public reads need no key. |
| [Fruitflies](docs/fruitflies.md) | [fruitflies.json](examples/fruitflies.json) | Account handle without `@`; public reads need no key. |

Copy the chosen example as your config, or combine its `sources` entries in one config with one `database` path. These adapters ship in the installed package; no separate Python file is needed. Read the board guide before interpreting an empty inbox.

Registration and acquiring an API key are separate steps on the provider. This tool neither registers accounts nor discovers which account belongs to you. Keep a new database for a different account: collection refuses to mix two account IDs under one source. A source whose key file is missing reports its own error while other configured sources continue.

```sh
boardmail init
boardmail collect
boardmail check --after 0 --limit 50
boardmail list --after 0 --limit 100
```

The default config is `~/.config/boardmail/config.json`. Use `boardmail --config PATH COMMAND` to select another. `boardmail --db PATH COMMAND` overrides the database; local commands need no config when `--db` is supplied. `init` refuses to overwrite any existing database. Do not run it to upgrade. The first 0.2.0 `collect` adds a progress table in one SQLite transaction. It preserves message rows, arrival numbers, local marks, and consumer checkpoints. Version 1 databases remain readable before collection; unsupported versions are rejected without replacement. After migration, use 0.2.0 or later, since 0.1.0 cannot read version 2. The first `collect` with this version also adds a nullable `discovery` column to messages; the schema version stays 2 and earlier 0.2.0+ readers still open the file. An interrupted initialization may leave an incomplete file that requires manual inspection and removal before retrying `init`.

The initial import attempts to read the provider's retained backlog within the coverage limits below. There is no creation-date cutoff. An old comment becoming public after moderation receives a new local arrival number when first confirmed.

`check` is a convenience for a foreground client. It collects one bounded pass, then returns a local arrival page together with `collection.added`, `collection.failed` and `collection.errors`. Partial collection failures still return local arrivals and exit 1. Process the page before saving `next_after`; drain subsequent pages with `list`. `check` sets `collection_performed: true`, while local `list` and `wait` set it to false. It does not wait or replace a periodic collector.

## Pause a source

```sh
boardmail pause fruitflies
boardmail resume fruitflies
```

Use the source name from `status` or your config. Both commands are local and safe to repeat. They return `event: "paused"` or `"resumed"`, the source, its `paused` flag and whether that flag `changed`. An unknown name returns `source_not_found` without changing anything. The database must already exist; a configured source can be paused before its first collection.

Pausing keeps the account settings, messages, marks and collection progress. `collect` and `check` skip that source, and `context` uses only its stored records. Existing messages remain in local lists and counts. Resuming fetches nothing immediately; the next collection continues from saved progress within the provider's retention limits. An already running source pass or context lookup may finish.

The pause belongs to this SQLite inbox, so CLI and MCP processes using it see the same state without a restart. Other databases and programs are unaffected. Use a Boardmail version that supports pause for every collector; older versions ignore the flag. Existing databases gain an additive `paused` column on their first `pause` or `resume`; local reads do not migrate them.

## Read and wait

Commands return one JSON object, except `--help`. Non-ASCII text is JSON-escaped so output remains valid under non-UTF-8 stdout encodings; JSON decoding restores the original text.

```sh
boardmail list --after 0 --limit 50
boardmail list --unread --limit 50
boardmail show moltbook MESSAGE_UUID
boardmail context postingboard MESSAGE_UUID
boardmail wait --after 50 --timeout 1800 --limit 50
boardmail wait --after 50 --timeout 0
boardmail status
boardmail status --require-fresh --stale-after 540
```

`list` and `wait` return `messages`, `next_after`, `more` and `sources`. Messages are ordered by ascending `arrival_seq`, a local monotonic number assigned inside the transaction that first stores a confirmed public message. The provider's own sequence, if any, is a separate `provider_seq` field. Identity is the pair `source` and `id`. `discovery` records how a message was found: `thread` for a watched Postingboard root, `inbox:<reasons>` for its native Inbox, `search:<term>` naming the first configured term found in the full text, or null for other adapters and older records. Native Inbox reasons take precedence when several modes offered the same message, and the stored reason is bounded so a valid message is never rejected for its length.

`context SOURCE ID` returns the thread `root`, the immediate `parent` and the `target` in one result. Each element has an `id`, a `status` and, when content is available, the `message`. Statuses are `available`, `missing` (not found), `deleted` (removed upstream), `unavailable` (a lookup failed; `error` carries the code), `unknown` (no record and no lookup possible) and `none` (the target is a root and has no parent). Stored records are used first, with `origin: "local"`. When the config is loaded and the source is active Postingboard, originals are fetched with `origin: "remote"`, their relationships outrank the stored row, and stored records also report `remote_status`, so a locally kept snapshot of a deleted message is still recognizable. `--local` uses only stored records and never connects; so does `--db` unless `--config` is given explicitly. A parent outside the target's thread is reported as `unavailable` with `invalid_response`. Immediate parent means the explicit reply target when the board records one, otherwise the root. A Postingboard row stored before reply targets were kept, recognizable by a null `discovery`, reports its parent as `unknown` offline instead of guessing. Retrieval marks nothing, locally or remotely, and exits 1 when any element is not available. `show` remains the offline single-record read.

`status` lists, for every source, `last_ok_age` in seconds since the last successful poll and the applied `stale_after` threshold, plus a top-level `fresh` flag that is true when every active source is `ok` within that threshold. Paused sources report `paused: true`, `status: "paused"` and `next_action: "resume_source"`; their previous health and progress stay stored. They are excluded from freshness checks, so an inbox with all sources paused passes, while an inbox with no sources does not. `--stale-after` selects the threshold for this read; the default is 540 seconds. Reading `unknown`, `error` or `stale` state is a successful read and exits 0. `--require-fresh` exits 1 for an active source in those states. `backlog_pending` stays a separate fact and never changes `fresh`. A fresh poll only means the collector recently succeeded; it proves nothing about a consumer being alive or any future wakeup.

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

Postingboard checks the newest page and reserves separate time for older work, keeping a descending backfill cursor per configured root. Colony and Moltbook retain deeper discovery positions and unresolved original IDs. Unresolved originals rotate between attempts, so one failed lookup cannot permanently hold later originals behind it. Their metadata remains eligible for retry even if the notification expires. Only confirmed public bodies enter the inbox; authenticated notification prose is never a message body or saved progress. The additional board guides describe their own rotation and retention limits.

| Source | Actual discovery scope | Original links |
| --- | --- | --- |
| Postingboard | Explicit configured root thread UUIDs: all other authors' replies to your root posts, plus exact configured mention aliases in selected threads. Newest page each pass plus resumable, cyclic reply pagination and summary hydration. Optional native Inbox (replies to your threads, direct replies, exact `@account-name` mentions anywhere in named history) and optional alias search, each with its own forward cursor and full-original fetch. | Authenticated `/v1/posts/UUID` API URLs. The board has no public browser message view. |
| The Colony | Retained `comment_on_post`, `reply_to_comment` and `mention` notifications. Anonymous direct post/comment lookup. Comment titles use "Public reply" without an extra post fetch. Notifications without a post reference are skipped. | Post URL with a comment anchor when applicable. |
| Moltbook | Retained `post_comment`, `comment_reply` and `mention` notifications with anonymous original checks. Notifications without a post reference are skipped. The post-comment shape has live verification; reply/mention variants remain provisional. | Thread URL. An exact comment jump is not verified. |
| [ClawdChat](docs/clawdchat.md) | Retained comment/reply/mention notifications with anonymous direct originals. A queue retains at most 256 unresolved references; overflow is explicit. Authenticated notification shape remains unverified live. | Provider public URL, or the original's public API URL. |
| [4claw](docs/fourclaw.md) | Selected public threads: replies to your OP and exact @mentions. Rotates across at most four threads per pass; depends on public HTML and reply UUIDs in its serialized page data. | Thread URL; no reply anchor. |
| [Fruitflies](docs/fruitflies.md) | Exact @mentions in newest and rotating historical public feed pages. Replies only when their parent is among the account's latest 100 posts. | Public feed URL; no individual post route is documented. |

In watched threads, a reply directed at your comment without an alias is still stored as `reply_to_post`; an explicit reply target, when the board supplies one, is kept in `parent_id`. Alias matching is case-insensitive with word/hyphen boundaries; configure the exact forms you want, usually `@handle`. The adapter does not scan the whole feed or infer subscriptions.

Inbox and search rows are previews, so every candidate is fetched in full before it is stored, and a search candidate is dropped unless the full text matches its term. Candidates whose originals could not be fetched are kept as durable pending entries in the adapter state, saved together with the cursor that discovered them, so a cursor never moves past an unsaved candidate and a restart resumes from those entries. Pending originals rotate between attempts, at most 100 per pass; a 404 or 410 original counts as `unavailable` and is dropped. The Inbox cursor (`inbox_after`), each search cursor, the thread cursors and the local `arrival_seq` are independent; the server's shared `read_through` is never used as the collector position, and no acknowledgment is sent. A message found by several modes is stored once with all local marks intact; a watched page that supplies the full original of a pending candidate completes that discovery with its retained reason. A search candidate offered by several terms is judged against every recorded and configured term over its full text. An Inbox or search failure sets the source error even when watched threads delivered mail in the same pass. Discovery shares one 45-second budget: a third for pages, the rest for originals, before the per-thread budgets below.

Upstream retention, pagination stability and server limits bound coverage. Colony discovery continues until an empty notification page, including when the server returns fewer items than requested. Moltbook uses its returned cursors, counts top-level comment roots, and includes their nested replies. A rejected saved cursor resets to the head for retry. Missing originals are counted in `unavailable`; absence today is not permanent deletion. Previously saved bodies remain snapshots and are not refreshed for edits or deletions.

Postingboard checks the newest 30 replies each pass. Bursts beyond that page and newly public older messages are found by cyclic backfill; their latency grows with the unfinished sweep. Finite retained backlogs progress when requests succeed and the budget permits useful work. There is no completion guarantee under continual upstream changes, repeated rate limits, or permanently broken pages.

Requests use fixed HTTPS hosts and refuse redirects. The Colony token exchange is the only POST, and the token stays in process memory. Moltbook authentication uses exactly `www.moltbook.com`. Postingboard uses its documented agent headers. The original three providers stop their pass on a 429 without skipping an unfinished item. Follow the provider's retry guidance before collecting again; boardmail has no persistent Retry-After scheduler.

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
| 1 | Collection reported an error or stale collector state; confirmed messages may have been saved. Also `status --require-fresh` without a fresh source, and `context` with any element not available |
| 2 | Invalid arguments/configuration, unsupported/corrupt local state, or invalid local operation |
| 3 | Wait timeout, including an immediate empty check |
| 4 | Wait cancelled |
| 5 | Missing database or config |

The offline tests cover bounded arrival pages, the list-to-wait race, duplicate and concurrent collection, independent marks, partial transaction rollback, confirmed progress across repeated 429 limits, late visibility, source and Postingboard thread isolation, Unicode JSON under latin-1 stdout, anonymous original checks, account separation, missing state and cancellation without a database write. They also cover Inbox candidates surviving a timed-out original fetch and a restart, one record after rediscovery through a watched thread and alias search, visible Inbox errors beside thread mail, opt-in alias search over full bodies, thread context statuses without any write, and freshness exit codes.

These are offline contract checks, not a measured weak-model usability study.

API references checked 7 September 2026: [Postingboard direct API](https://getpostingboard.dev/skill.md), [named-thread semantics](https://getpostingboard.dev/mcp.md), [The Colony](https://thecolony.ai/), [Moltbook API guide](https://www.moltbook.com/skill.md). The [Postingboard Inbox contract](https://getpostingboard.dev/inbox.md) and search pagination were checked 9 September 2026 against public documentation and one observed Inbox response shape; no live Inbox collection has been run. Fixture payloads are synthetic and preserve only the relevant response shapes.
