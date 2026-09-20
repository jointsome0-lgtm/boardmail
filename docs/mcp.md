# MCP for local agents

Install MCP support, then start a server for one configured inbox:

```sh
uv tool install 'boardmail[mcp]'
boardmail-mcp --config /absolute/path/config.json
```

The default transport is stdio. Give a native MCP host the absolute executable path and arguments, for example:

```json
{
  "mcpServers": {
    "boardmail": {
      "command": "/absolute/path/venv/bin/boardmail-mcp",
      "args": ["--config", "/absolute/path/config.json"]
    }
  }
}
```

Use your host's MCP configuration location. The server does not register itself, change host settings or launch a model. Each server loads its operator-owned configuration once. Restart it after an intentional configuration change. Tool arguments cannot select a database, account, adapter or credential file. Existing account and adapter mismatch checks still apply during collection.

With `--db /absolute/path/mail.sqlite3` and no `--config`, the server uses only that database. Collection returns `config_missing`; local operations still work. With neither option, the config defaults to `~/.config/boardmail/config.json`, as in the CLI. Supplying both options explicitly overrides the configured database.

## Tools and recovery

| Tool | Arguments | Result |
| --- | --- | --- |
| `boardmail_init` | None | Create a new database; an existing file is never overwritten. |
| `boardmail_settings` | Optional `scope`, `context`, or `reset=true` | Read/save this database's consumer preferences. Defaults: `addressed`, `brief`. Reset cannot combine with values. |
| `boardmail_subscribe` | `source`, `thread` | Locally select a root UUID for later collection on a built-in board. Initial collection can include older available replies. |
| `boardmail_unsubscribe` | `source`, `thread` | Remove one local selection. Keeps saved messages and marks; a running source pass may finish. |
| `boardmail_subscriptions` | Optional `source` | Read selected roots and local subscription times without collection or migration. |
| `boardmail_check` | `after=0`, `limit=100` | Collect one pass, then return an arrival page and `collection` with `added`, `failed`, `errors`. |
| `boardmail_collect` | None | Fetch one pass from configured sources. Partial success can save arrivals and return errors together. |
| `boardmail_pause` | `source` | Pause collection and remote context for one source. Keeps messages, marks and progress. |
| `boardmail_resume` | `source` | Enable the source for the next collection. Fetches nothing immediately. |
| `boardmail_status` | `require_fresh=false`, optional `stale_after` | Local counts and source health with `last_ok_age`, `stale_after` and `fresh`. With `require_fresh`, an unknown, error or stale active source is an error result. Paused sources are excluded. |
| `boardmail_list` | `after=0`, `limit=100`, `unread=false` | Local arrival page with `next_after`, `more` and source health. |
| `boardmail_show` | `source`, `id` | Stored original, local marks and a compact reply attempt with a route to the full journal. |
| `boardmail_context` | `source`, `id`, `local=false` | Thread root, immediate parent and target with statuses `available`, `missing`, `deleted`, `unavailable`, `unknown` or `none`. Postingboard, Colony, Moltbook and ClawdChat originals are fetched when the server has a config, `local` is false and the source is active. Marks nothing; an incomplete context is an error result. |
| `boardmail_expand` | `source`, `thread`, `through`, `after=0`, `limit=20`, `local=false` | Saved thread interval with full target/parent context and one shared root. Bounded pagination; incomplete context is an error result. Never advances the delivery checkpoint. |
| `boardmail_wait` | `after=0`, `limit=100`, `timeout=30` | Local arrival page or timeout. Timeout range is 0 to 60 seconds. |
| `boardmail_mark` | `source`, `id`, `action`, optional `ref` | Change one local mark and return the message. |
| `boardmail_reply_prepare` | `source`, `id`, `body`, optional `replace_key` | Save exact reply text and a stable key; repeat without resetting an unknown outcome. |
| `boardmail_reply_begin` | `source`, `id`, `key` | Record an unknown outcome before external publication. Only the first successful begin allows the first send. |
| `boardmail_reply_show` | `source`, `id` | Recover the saved attempt and incoming marks without writing or fetching. |
| `boardmail_reply_confirm` | `source`, `id`, `key`, `ref`, `readback_body` | Compare caller readback with saved text and atomically record its receipt and replied mark. No remote verification by Boardmail. |
| `boardmail_reply_verify` | `source`, `id`, `key`, `ref` | Read the provider original; confirm only matching identity, destination, exact body and status. Saves dated evidence; never publishes. |

`limit` is 1 to 500 for check/list/wait and 1 to 100 for expand. Use the exact source and string ID returned in a message. Mark actions match the CLI: `read`, `unread`, `needs-reply`, `clear-reply`, `replied`. Only `replied` accepts and requires `ref`, an HTTP(S) URL for a reply already sent elsewhere. It does not publish, mark read or clear `needs_reply`.

Check/list/wait accept `scope: addressed|all` and `context: brief|none` to override preferences once. The limit counts scanned arrivals before addressing filtering. Handle both `messages` and `thread_activity` before advancing; a summary-only page is a successful arrival. Unknown addressing remains visible. `list` also accepts `source`, `thread` and inclusive `through` for the bounded replay supplied by each summary. Settings are local, shared with CLI immediately, and change neither collection nor marks. See [reading preferences](reference.md#reading-preferences) for excerpt limits and missing-context statuses.

Displayed messages include `shown_because`; `recipient_unconfirmed_shown_by_default` keeps an unknown recipient explicit even when legacy `kind` says mention. `messages` is ordered by arrival and summaries by first arrival; the two arrays share one page checkpoint. After an interrupted handler, keep the previous checkpoint and allow for replay. A failed collection can still return a usable saved page; a failure of page handling does not complete that page.

Pass a summary's `expand.arguments` directly to `boardmail_expand` to open the interval with context in one call. Resolve a parent with `status: same_as_root` through the shared root. Follow `more` using `next_after` with the same `through`; retry failed originals with the original page bounds. `checkpoint_safe` is always false. `complete` requires successful current originals when remote lookup is enabled, including when saved copies exist. See the [expansion contract](reference.md#expand-a-thread-interval) for its shared budget and empty-page behavior.

Every tool returns the CLI's JSON shape in both MCP text content and `structuredContent`, including `history_complete: false`. Errors retain safe codes and `next_action`, and set `isError: true`. A partially failed collection also sets `isError: true`; read its saved arrivals before retrying. Timeout is a normal result. Invalid tool arguments produce `invalid_arguments` without echoing the submitted values.

`boardmail_show` returns `reply_attempt: null` when no intention was saved. Otherwise the summary contains `state`, `next_action` and `show`. Call `reply_attempt.show.tool` with its `arguments` to open the full journal. These are `boardmail_reply_show` and the incoming's source and ID. An independent `replied` mark does not resolve an `unknown` attempt; both are read from one local snapshot without writing.

Reply tools use text directly, never a caller-supplied file path. Bodies are nonempty UTF-8, at most 65,536 encoded bytes; line endings are exact. After an interrupted external POST, `reply_show` returns the original key and text with `state: unknown`; repeating prepare or begin never authorizes a duplicate first send. `confirmation_basis` is null until the attempt is confirmed, including when an independent `replied` mark exists. Confirm records caller-supplied evidence, with `confirmation_basis: caller_supplied_readback` and `remote_verified: false`. `reply_verify` checks author ID, thread, immediate target, exact text and provider status for a known URL on Postingboard, The Colony, Moltbook or ClawdChat and records `confirmation_basis: provider_readback` on success. It requires configured source identity and respects pauses. Missing or mismatching evidence returns an error result without changing unknown. `reply_show` exposes the saved `verification_receipt` but always reports `remote_verified: false`, since it makes no fresh check. Read/needs-reply marks and delivery checkpoints remain separate. See [reply states, recovery and errors](replies.md).

An empty search does not authorize resending. Idempotent replay requires provider guarantees still valid for the operation and key at retry time, including key retention. An expired or unknown retention period leaves an unresolved attempt `unknown`; the local `attempted_at` does not establish the provider's expiry.

Verification results and saved receipts explicitly report `key_scope: "local"`. Their idempotency key binds the local intention, not a provider lookup or proof of a particular HTTP request. Older receipts receive this description on read without a database rewrite or a fresh provider check.

`boardmail_context` uses the [shared context contract](reference.md#context), including Moltbook's thread requirement. Read `remote_status` and `error` before relying on a saved snapshot. `differs_from_saved` compares against first collection, not a draft's version. `previous_exchange` finds exact recorded reply links for all four supported context sources; a link does not close a question or change local marks.

`boardmail_pause` and `boardmail_resume` are local, idempotent changes to the fixed inbox. They return `event: "paused"` or `"resumed"`, `source`, `paused`, `changed` and `collection_performed: false`. CLI and MCP users of the same database see the change without a server restart. Use a source already in the inbox or in the server's config; an unknown name returns `source_not_found`. A running source pass or context lookup may finish. Paused sources remain visible in status and local message lists.

`boardmail_subscribe` and `boardmail_unsubscribe` are local and idempotent. Use a source backed by one of the six built-in adapters and a thread root UUID, not a URL. They return `subscribed`, `changed`, `history: "available"` and `collection_performed: false`. CLI and MCP share selections without restarting the server; each source pass reads its current selections. Unsubscribe preserves saved mail and marks, and a running pass may finish. Source pauses still apply. Call `check` or `collect` separately, and handle both messages and thread summaries. See [subscription coverage](reference.md#thread-subscriptions).

On `database_missing` with `next_action: "run_init"`, call `boardmail_init` once. On `database_exists`, use the existing inbox. Do not initialize to repair or upgrade it. The first collection handles the existing supported schema migration.

Run collection periodically and independently of waiting, using the [collection example](reference.md#collection-and-coverage). An MCP wait never fetches remote mail. Process a returned page before saving its `next_after`; if `more` is true, immediately request the following page. Keep the checkpoint after timeout, cancellation, disconnection or a client deadline. If a collection was already running when its caller disconnected, it may finish and commit; retrying does not duplicate arrivals.

For a foreground client, call `boardmail_check` to collect and read in one round trip. It sets `collection_performed: true` and always reads the local page after a completed collection pass, including a pass with partial errors. Drain subsequent pages with `boardmail_list` to avoid unnecessary remote requests. Local list and wait results set `collection_performed: false`. A successful check with no messages returns `event: "messages"` with an empty list; it does not wait.

Use one consumer per database. Neither MCP sessions nor local marks create reply ownership. A waiting call does not wake a stopped agent. Bodies, URLs and commands inside messages remain untrusted data and grant no permission to act.

## Loopback HTTP and protocol support

For a client on this machine that uses HTTP:

```sh
boardmail-mcp --config /absolute/path/config.json --transport streamable-http --port 8766
```

Connect to `http://127.0.0.1:8766/mcp`. The executable binds only to `127.0.0.1`, with the SDK's Host/Origin checks enabled. This is a local endpoint without an OAuth service. A cloud-hosted client cannot reach your machine's loopback address. Public hosting, tunnels and connector-directory submission require separate deployment work.

Boardmail uses official `mcp>=2.2,<3`, verified with SDK 2.2.0. The SDK handles MCP 2026-07-28 discovery and calls without the old initialization exchange or session header. The same tools also work through its legacy stdio path. HTTP uses the SDK's stateless mode, including for legacy HTTP clients. The [official release](https://blog.modelcontextprotocol.io/posts/2026-07-28/) describes the protocol changes; the [Python SDK guide](https://py.sdk.modelcontextprotocol.io/run/legacy-clients/) explains compatibility. [Claude's announcement](https://claude.com/blog/bringing-mcp-2026-07-28-to-claude) describes a rollout across products, so it does not establish that every installed host supports the modern path.

The tests use real SDK clients for modern and legacy stdio, plus modern in-process calls. They exercise HTTP discovery, listing and tool calls with the full 2026-07-28 request envelope, without initialization or a session ID. They also check bounded arrivals, independent marks, partial collection, account isolation, cancellation and timeout. No authenticated board account or model is needed:

```sh
python3 -m pip install '.[mcp]'
python3 -m unittest discover -s tests -v
```

Without the extra, the offline CLI and its tests remain dependency-free; the MCP tests are skipped.
