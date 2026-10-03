# Changelog

## 0.14.1, 2026-10-03

This release fixes collection continuity, saved-state handling and agent-facing diagnostics.

- Cached originals now advance collection revisions even on a partial failed pass, so a competing stale collector cannot replace that newer cache. Subscription changes reject a mismatched adapter for legacy sources whose identity predates adapter bindings.
- Colony uses numbered comment pages in oldest-first order and checks the returned page number. Reaching the per-pass page cap preserves the next page, allowing later passes to continue beyond page 100 rather than ending the thread scan there.
- ClawdChat keeps retained parent pages separate from newly discovered children, allowing bounded scans to continue across passes. An unavailable nested parent no longer blocks healthy siblings; an unexpectedly empty page keeps its offset for retry. Queue bounds and upstream changes can still leave gaps.
- Botnet reports a budget stop after successful identity checks as incomplete progress rather than a source failure. Failed identity checks and actual provider errors remain errors.
- Postingboard can resolve retained discoveries from originals already fetched in watched threads. Addressing uses confirmed ownership from the same pass; absent author evidence stays unknown. Full public context does not attach saved relatives from a conflicting thread.
- Invalid custom-adapter state serialization returns `invalid_adapter_result`. Local command errors add identifier and read-only recovery guidance where applicable; a failed reply command does not authorize publication. CLI and MCP help now reflect adapter support and saved reply semantics.
- Initialization with explicit `--config` validates and seeds configured source identities even with a `--db` override. Using `--db` alone for `init` still creates an empty inbox without loading config. Existing inboxes must not be reinitialized.
- The release checker rebuilds unpacked source distributions and checks installed direct and rebuilt wheels using the archive's own fixtures. It records module and entry-point origins, preserves partial timeout output and reports actual temporary-state cleanup. Recovery guidance for a damaged optional consumer ledger preserves the ledger, checkpoint and inbox rather than silently discarding observations.

Regression coverage includes concurrent persistence and reply transitions, migration rollback, paused subscriptions and MCP startup, cancellation, pipe EOF and noisy collection. Installed direct and sdist-rebuilt wheels were checked outside the checkout with bundled fixtures. Provider regression coverage uses synthetic responses; these checks do not establish complete live history or MCP lifecycle cleanup on every host and operating system.

### Updating and rollback

Stop collectors and long-lived MCP servers before replacing their environment. With writers stopped, back up the config and SQLite database; also preserve any optional consumer ledger and delivery checkpoint. For an unpinned registry installation:

```sh
uv tool upgrade boardmail
```

To replace a Git checkout, local wheel or old version constraint with a registry installation that allows future upgrades, choose the command matching your installation:

```sh
uv tool install --force 'boardmail>=0.14.1'
# With MCP support:
uv tool install --force 'boardmail[mcp]>=0.14.1'
```

Check `uv tool list`, `boardmail --help` and, when installed, `boardmail-mcp --help`. Inspect the retained inbox with `boardmail --db /path/to/inbox.sqlite3 status`, substituting its actual path, then restart the collector or MCP server.

Version 0.14.1 introduces no new SQLite schema version and requires no `init` for an existing inbox. The update retains saved mail, marks, subscriptions and pauses. ClawdChat continuation state adds deferred pages and loads older checkpoints, including a retained 21st parent. Upgrade every collector sharing that inbox before resuming; older collectors do not preserve deferred progress.

For a package rollback, stop all writers again, preserve the current state and reinstall `boardmail==0.14.0` (or `boardmail[mcp]==0.14.0`) with `uv tool install --force`. The older package can read the same database format, but continuation from a ClawdChat checkpoint written by 0.14.1 is not guaranteed. Do not alternate collector versions or use `init` as a repair. If you restore a pre-upgrade snapshot, keep the current files too: that snapshot excludes later arrivals, marks and checkpoints, which require separate reconciliation. See [ClawdChat continuation](docs/clawdchat.md#subscribed-threads) and [optional-ledger recovery](docs/reference.md#recover-a-damaged-optional-ledger).

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
