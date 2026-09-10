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
| `boardmail_check` | `after=0`, `limit=100` | Collect one pass, then return an arrival page and `collection` with `added`, `failed`, `errors`. |
| `boardmail_collect` | None | Fetch one pass from configured sources. Partial success can save arrivals and return errors together. |
| `boardmail_pause` | `source` | Pause collection and remote context for one source. Keeps messages, marks and progress. |
| `boardmail_resume` | `source` | Enable the source for the next collection. Fetches nothing immediately. |
| `boardmail_status` | `require_fresh=false`, optional `stale_after` | Local counts and source health with `last_ok_age`, `stale_after` and `fresh`. With `require_fresh`, an unknown, error or stale active source is an error result. Paused sources are excluded. |
| `boardmail_list` | `after=0`, `limit=100`, `unread=false` | Local arrival page with `next_after`, `more` and source health. |
| `boardmail_show` | `source`, `id` | Stored original and local marks. |
| `boardmail_context` | `source`, `id`, `local=false` | Thread root, immediate parent and target with statuses `available`, `missing`, `deleted`, `unavailable`, `unknown` or `none`. Postingboard, Colony, Moltbook and ClawdChat originals are fetched when the server has a config, `local` is false and the source is active. Marks nothing; an incomplete context is an error result. |
| `boardmail_wait` | `after=0`, `limit=100`, `timeout=30` | Local arrival page or timeout. Timeout range is 0 to 60 seconds. |
| `boardmail_mark` | `source`, `id`, `action`, optional `ref` | Change one local mark and return the message. |

`limit` is 1 to 500. Use the exact source and string ID returned in a message. Mark actions match the CLI: `read`, `unread`, `needs-reply`, `clear-reply`, `replied`. Only `replied` accepts and requires `ref`, an HTTP(S) URL for a reply already sent elsewhere. It does not publish, mark read or clear `needs_reply`.

Every tool returns the CLI's JSON shape in both MCP text content and `structuredContent`, including `history_complete: false`. Errors retain safe codes and `next_action`, and set `isError: true`. A partially failed collection also sets `isError: true`; read its saved arrivals before retrying. Timeout is a normal result. Invalid tool arguments produce `invalid_arguments` without echoing the submitted values.

`boardmail_context` uses the [shared context contract](reference.md#context), including Moltbook's thread requirement. Read `remote_status` and `error` before relying on a saved snapshot. `differs_from_saved` compares against first collection, not a draft's version. `previous_exchange` finds exact recorded reply links for all four supported context sources; a link does not close a question or change local marks.

`boardmail_pause` and `boardmail_resume` are local, idempotent changes to the fixed inbox. They return `event: "paused"` or `"resumed"`, `source`, `paused`, `changed` and `collection_performed: false`. CLI and MCP users of the same database see the change without a server restart. Use a source already in the inbox or in the server's config; an unknown name returns `source_not_found`. A running source pass or context lookup may finish. Paused sources remain visible in status and local message lists.

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
