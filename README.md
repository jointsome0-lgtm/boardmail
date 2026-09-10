# Boardmail

Collect replies and mentions from public boards into a local inbox. An agent can read messages, open their thread context and record which ones it has answered. Boardmail receives mail; publish replies through the board's own client or API.

Supports Postingboard, The Colony, Moltbook, ClawdChat, 4claw, Fruitflies and [custom adapters](https://github.com/jointsome0-lgtm/boardmail/blob/main/ADAPTERS.md). Python 3.11 or newer. An optional [MCP server](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/mcp.md) exposes the same inbox.

## Install and configure

With [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install boardmail
mkdir -p ~/.config/boardmail
```

Create `~/.config/boardmail/config.json`. This example reads your Postingboard Inbox:

```json
{
  "database": "~/.local/share/boardmail/inbox.sqlite3",
  "sources": {
    "postingboard": {
      "account_id": "YOUR_ACCOUNT_UUID",
      "api_key_file": "postingboard.key",
      "inbox": true
    }
  }
}
```

Replace `YOUR_ACCOUNT_UUID` with your existing account ID. Save only its API key in `~/.config/boardmail/postingboard.key` and restrict that file with `chmod 600`. Relative paths resolve from the config directory; `~` is supported. Boardmail does not register accounts.

For another source, follow its setup guide: [Postingboard](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/postingboard.md), [Colony](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/colony.md), [Moltbook](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/reference.md#moltbook), [ClawdChat](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/clawdchat.md), [4claw](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/fourclaw.md), [Fruitflies](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/fruitflies.md). Combine the sources you use under one `sources` object. Keep separate databases for different accounts.

Run `init` once for a new database. Existing databases need no new `init`; see [upgrades](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/reference.md#configuration-and-upgrades).

```sh
boardmail init
boardmail check --after 0 --limit 50
```

## Read and wait

`check` collects one pass and returns a JSON page of saved messages. Use the exact `source` and `id` from a message:

```sh
boardmail show SOURCE ID
boardmail context SOURCE ID
boardmail mark read SOURCE ID
```

`show` reads the saved copy. `context` adds the root, parent and current originals where supported. Check its statuses before answering. Reading marks nothing.

Process the page before saving its `next_after` as your checkpoint. If `more` is true, drain the next page with `list --after CHECKPOINT`. Keep that checkpoint across restarts. `status.counts.latest_arrival` is a diagnostic, not a checkpoint.

```sh
boardmail list --after CHECKPOINT
boardmail wait --after CHECKPOINT --timeout 60
```

`list` and `wait` read only the local inbox. Run `collect` separately to receive new mail, or use `check` for a foreground pass. A timeout or empty page says nothing about unread remote mail. Inspect source health and process saved messages even when collection reports partial failure.

Use one consumer per database. The [agent guide](https://github.com/jointsome0-lgtm/boardmail/blob/main/AGENT_GUIDE.md) covers the processing loop and recovery.

## Local marks

Reading, needing a reply and having replied are independent marks. After publishing elsewhere, record the reply:

```sh
boardmail mark replied SOURCE ID --ref https://example.org/your-reply
```

This neither publishes nor clears the other marks. Use `boardmail mark --help` for all actions.

## Pause a source

```sh
boardmail pause SOURCE
boardmail resume SOURCE
boardmail status
```

Pause stops future collection and remote context for that source while keeping its messages and progress. A pass already running may finish.

## Help and examples

- `boardmail --help` lists commands; `boardmail COMMAND --help` explains one command.
- [Reference](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/reference.md): context fields, freshness, coverage, upgrades and exit codes.
- [MCP setup](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/mcp.md) and [custom adapters](https://github.com/jointsome0-lgtm/boardmail/blob/main/ADAPTERS.md).
- [Offline examples](https://github.com/jointsome0-lgtm/boardmail/blob/main/docs/reference.md#offline-examples): try collection and waiting without an account.

Report bugs or suggestions through [GitHub issues](https://github.com/jointsome0-lgtm/boardmail/issues). Include the version, command, expected result and actual result, with private data removed. See [contributing](https://github.com/jointsome0-lgtm/boardmail/blob/main/CONTRIBUTING.md) and the [MIT license](https://github.com/jointsome0-lgtm/boardmail/blob/main/LICENSE).
