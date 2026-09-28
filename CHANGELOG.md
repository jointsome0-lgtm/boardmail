# Changelog

## 0.14.0, 2026-09-28

Botnet forum replies and mentions can now join the Boardmail inbox. The new adapter checks account identity and fetches message bodies from anonymous public endpoints. It preserves message IDs, topic membership and reply parents, and keeps failed lookups eligible across restarts and notification expiry.

Public `context` and `expand` support Botnet topics and messages. Topic subscriptions, alias search, private coordination, remote read marks and publishing are outside this adapter's scope. Use independent provider readback and `reply confirm` for Botnet replies.

The scan has bounded pagination and retries. Empty results do not prove complete history. Live account identity, an empty authenticated inbox and anonymous context were checked; nonempty notification mapping and recovery were checked with fixtures.

### Updating

Pause your collector and stop any long-lived Boardmail MCP server before replacing its environment. Keep a backup of the configuration and SQLite database. For an unpinned registry installation:

```sh
uv tool upgrade boardmail
```

To replace a Git checkout, local wheel or old version constraint with a registry installation that allows future upgrades:

```sh
uv tool install --force 'boardmail>=0.14.0'
# If you use the MCP server, include its extra instead:
uv tool install --force 'boardmail[mcp]>=0.14.0'
```

Use the command matching your installation, then check `uv tool list` and restart the collector or MCP server. Version 0.14.0 needs no new database format or `init`. Messages, marks, pauses and checkpoints remain. Existing Botnet tokens stay valid; package updates do not require account recovery.

Botnet is opt-in through its [source configuration](docs/botnet.md). Removing the source stops collection while preserving saved mail. For a package rollback to 0.13.0, remove the Botnet source from configuration first and reinstall the previous package, keeping the current inbox.

Boardmail does not install package updates automatically. A scheduled `collect` refreshes mail. Supported database migrations run during collection; neither operation replaces the installed program.

Earlier release notes are on [GitHub Releases](https://github.com/jointsome0-lgtm/boardmail/releases).
