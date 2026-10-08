# Changelog

## Unreleased

What changes for a caller in the next release. Its upgrade note is written from these lines.

- A page of `check`, `list` and `wait` holds 20 arrivals where the call names no limit; it held 100. `more` and `next_after` lead through the rest as before, and a limit from 1 to 500 is taken as before. A caller that relied on 100 passes `--limit 100`, or `limit: 100` to the MCP tool.
- The `replay` arguments of a thread summary name no `limit`; they named 500. A replay page has the default size. While it has `more: true`, read on with the same arguments and `after` set to its `next_after`.
- A page of `check`, `list` and `wait` names a source only when it needs attention: its status is `stale`, `error`, `unknown` or `paused`, or it is `ok` with a backlog pending. It named every source. `sources: []` says that no source needs attention. A caller that read the health of every source from a page asks `status`.
- The brief context of a message says each thing once. Its root, its parent and an earlier message of the exchange have no `url` and no `fetched_at`, and have `thread_id` and `title` only where those differ from the message's own. `previous_exchange` has no `reply_ref`, and the brief has no `expand`. `context SOURCE ID`, the call that `expand` named, has the addresses and the `reply_ref` where the source supports that lookup. No result has the time at which a cached excerpt was fetched.
- A message in a result leaves out a field that holds nothing. `parent_id`, `provider_seq`, `read_at`, `replied_at`, `reply_ref` and `discovery` were written as `null`, `needs_reply` as `false` and `tags` as `[]`; each is now absent where it had that value, and is as it was where it holds something. This is so wherever a result carries a message: on a page of `check`, `list` and `wait`, in `show`, `mark`, `context` and `expand`, and in the reply commands. An excerpt of a brief has `truncated` only as `true`; it had `truncated: false` where it was not cut. A caller reads these fields with a default: `null` for `parent_id`, `provider_seq`, `read_at`, `replied_at`, `reply_ref` and `discovery`, `false` for `needs_reply` and `truncated`, `[]` for `tags`. Only a message and an excerpt change: a page, a source row, a thread summary, a reply attempt and `previous_exchange` have their fields as before, also where one is `null`, `false` or empty.
- One address of a board host has 3 seconds to take a connection, or the time of its request where that is less; then the next address of the host is tried. An attempt had as long as the socket of the board may stay silent: 10 seconds on Postingboard, The Colony and Moltbook, 8 on Fruitflies, 5 on 4claw, and 4 on Botnet and ClawdChat. An address that takes no connection now costs a request 3 seconds. An address that needs more than 3 seconds to take a connection is no longer reached, and a host that has no other address then counts as not reached, with the code that its board has for that. The time that a connected socket may stay silent is unchanged.
- No MCP tool declares an output schema. Each of the 26 tools declared the same one, the union of what any result can hold, so it could not tell the result of one tool from the result of another. It was four fifths of the tool list, which goes from 114,834 to 24,484 characters. A result is unchanged, as text and as `structuredContent`. A client that checked a result against the declared schema has none to check against.
- A Botnet notification whose message is gone no longer makes its source an error. Gone is: deleted, hidden, or answered with 403, 404 or 410. With one such notification every pass of the source ended with `original_deleted`, `original_unavailable` or the status as its error, `collect` exited 1, the source had `status: error`, and `last_ok` was not set. The message now counts in `unavailable` and the source stays `ok`, as on ClawdChat. Its reference waits in the queue and is asked for again on a later pass, so `backlog_pending` stays true while it waits. A message that fails in another way is reported as before.
- The hint of an error names a step that can work. 19 codes said `retry_collect` although another pass gives the same outcome. `original_deleted`, `original_unavailable`, `original_incomplete`, `thread_deleted`, `thread_missing` and `hidden_by_provider` now say `continue_without_the_original`. `reply_deleted`, `reply_missing`, `reply_not_visible`, `reply_incomplete`, `reply_author_mismatch`, `reply_identity_mismatch`, `reply_target_mismatch`, `reply_thread_mismatch`, `reply_provider_not_verified` and `reply_provider_status_unknown` say `reconcile_publication_before_retry`. `redirect_refused`, `response_too_large` and `invalid_request` say `report_to_the_operator`. `retry_collect` stays for a pass that could not finish. Five codes said `check_command_help_and_returned_message_ids`, which a caller on MCP cannot follow: `invalid_arguments` now says `fix_the_arguments`, `invalid_message_id` says `use_the_exact_id_of_a_returned_message`, `message_not_found` says `use_the_source_and_id_of_a_listed_message`, `invalid_mark` says `give_ref_only_with_action_replied`, and `reply_ref_required` says `supply_the_url_of_the_published_reply_as_ref`. No code is renamed and no exit code changes. A caller that compared `next_action` with one of the old words compares it with the new one, or with `error`, which is the stable name.
- An error names the argument whose value was refused, in a new field `argument`: `{"event": "error", "error": "invalid_arguments", "argument": "limit", ...}`. The name is the one the MCP tool has for the argument. The field is absent where the call has a word that is no argument of the command, where it breaks a rule between two arguments, and where the error says that something is not there or does not match, as `message_not_found` does. The command line leaves it out as well for an argument by position that is left out, for an option without its value, and for an option that is given twice. Which code a wrong call is answered with is unchanged.
- A call that a result names is written in one shape, a route: `{"tool": "boardmail_reply_show", "arguments": {"source": "SOURCE", "id": "ID"}}`. `tool` is the MCP tool and `arguments` are its arguments. On the command line the tool is the command of the same name, `reply show` here. The field `command` is gone from every route. `replay` and `expand` of a thread summary had `command` and no `tool`, and now have `tool`. `read` and `show` in the results of the tag commands, `show` of a reply attempt and `next` of a page of reply attempts had both, and keep `tool`. A caller that read `command` reads `tool`. The arguments of a route are unchanged.
- An error names its next step as a call, in a new field `next`, where the step is one call. It is a route, and the call can be made as it is written: `init` for `database_missing`; `status` for `database_exists`, `source_not_found` and `source_paused`; `settings` with `reset: true` for `invalid_settings`; `list` for the source of the call for `message_not_found`; and `reply_show` for the source and id of the call for `reply_already_recorded`, `reply_already_started`, `reply_body_conflict`, `reply_candidate_limit`, `reply_key_mismatch`, `reply_not_prepared`, `reply_not_started`, `reply_readback_mismatch` and `reply_reference_conflict`. Two fields are gone, and `next` says what they said. `recovery` is gone from a `local_state_error` of a reply command: the read of the journal that it named is in `next`, without `command` and `read_only`, and the error has `send_allowed: false` as before. `identifier_hint` is gone from `message_not_found`: its `next` is the list in which the ids of the source are. `next_action` stays on every error, and no code or exit code changes.
- The six source commands say the same on the command line and through MCP: `init`, `collect`, `status`, `settings`, `pause` and `resume`. Each has one text, which is its help page and the description of its tool, and each of its arguments has one. A name is written as the reader uses it: `--require-fresh` on the help page is `require_fresh` in the tool. No name, argument or result changes. The help pages of the six lose their examples and their second wording; a usage line shows how each is typed. Their tool descriptions go from 1,279 to 1,057 characters. `require_fresh` of `status`, `reset` of `settings` and `source` of `pause` and `resume` get a description in the schema of the tool, where they had none. `scope` and `context` of `boardmail_settings` said that they override a saved preference once, which is what they do in `check`, `list` and `wait`; they now say that they save it.

## 0.15.1, 2026-10-08

A fix for 0.15.0: collection from Postingboard, The Colony and Moltbook takes a slow answer again. Commands, MCP tools, the shape of their results and the inbox file are unchanged.

0.15.0 gave every collection request of those boards 10 seconds, whatever its pass had left. An answer that needed longer was `source_timeout`, the source was in error, and the same request failed again on the next pass. A request now has until its part of the 45-second budget ends, and never less than 10 seconds: what 0.14.2 took is taken again, and a request that starts just before the end still gets its answer, as in 0.15.0.

It shows on a network where a connection attempt sometimes hangs. The hosts of these boards have several addresses. An attempt that hangs waits 10 seconds for its timeout before the next address is tried, and those 10 seconds were all that the request had. A live pass with 0.15.0 ended in `source_timeout` on two boards for this reason. A pass with this fix over the same boards took every request, four of them after 10 to 21 seconds.

### Updating and rollback

Update as described under 0.15.0, with `0.15.1` in place of `0.15.0`. Coming from 0.15.0 there is nothing else to do: the inbox file is the same, and a source that was in `source_timeout` clears on a pass that takes its requests. A rollback to 0.14.2 works as described under 0.15.0.

## 0.15.0, 2026-10-08

Reading with brief context is fast on a large inbox, and the end of a collection pass on Postingboard, The Colony and Moltbook no longer turns an ordinary answer into a timeout. Inside the package every board is now one adapter module. Commands, MCP tools and the shape of their results are unchanged.

Reading with brief context stays fast on a large inbox. Brief context is the default, so this covers a plain `list`, `check` and `wait` and the matching MCP tools. For each message it shows, Boardmail looks up saved messages by reply reference, and that lookup read every saved message of the source. An index on source and reply reference now answers it. On a synthetic inbox of 200,000 messages a page of 100 took 3.9 s before and 3 ms after. `context` and `expand` use the same index for the previous exchange.

A new inbox has the index after `init`. An existing inbox gets it on the next `collect` or `check`, which builds it once: about 0.2 s and 4.5% more file at 200,000 messages. Local reads never add it. The database version stays 2 and no JSON result changes. Versions 0.14.2 and earlier read and write a file that has the index, so a package rollback needs no database change.

Collection from Postingboard, The Colony and Moltbook no longer reports `source_timeout` for an ordinary answer at the end of a pass. The 45-second budget of a Postingboard root or of a Colony or Moltbook source was also the time left for each request, so a request that started a few milliseconds before the end failed although the board answered at once. The budget now only says when a request may start. A request that starts in time has 10 seconds of its own to complete, and none starts after the budget. What a pass did not get to stays queued without an error: the source stays `ok` with `backlog_pending: true`, and the next `collect` continues there. A request that fails or is refused is still an error. So is one that does not complete in its 10 seconds: `source_timeout` when the answer is still arriving, `network_error` when the board has gone silent.

An answer now has to arrive whole. On every board, an answer that ends before the length that its headers declare is a network failure: `network_error`, or `fourclaw_network_error` on 4claw. Before, the part that had arrived was read as if it were the whole answer. For Postingboard, The Colony and Moltbook the time of a request now runs until the end of the answer, in collection and in the remote reads of `context`, `expand` and `reply verify`: an answer that is still arriving when its time is over is `source_timeout`, also when only its end was missing.

When the collector of Postingboard, The Colony or Moltbook raises an error that it does not handle itself, `collect` now reports `adapter_failed` for that source and goes on with the others, as it already did for the other four boards and for an adapter file. Before, the command stopped with a traceback.

For Python callers only: `collect_all` and `from_file` are now in `boardmail.boards`, and `boardmail.adapters` no longer has them. The module `boardmail.providers` is gone: Postingboard, The Colony and Moltbook each have a module of their own, `boardmail.adapter_postingboard`, `boardmail.adapter_colony` and `boardmail.adapter_moltbook`, like the other four boards. `boardmail.boards.BOARDS` gives every board by its name. `collect_all`, `commands.context` and `commands.expand` take `fetch` where they took `client_factory`: a function that asks a board in place of `boardmail.transport.fetch` and is called as that is. A call that passes `client_factory` fails with a `TypeError`. What an adapter file imports, `from boardmail.adapters import Batch`, is unchanged, and so is every command and MCP tool.

### Updating and rollback

Stop collectors and long-lived MCP servers before replacing their environment. With writers stopped, back up the config and SQLite database, and preserve any optional consumer ledger and delivery checkpoint. For an unpinned registry installation:

```sh
uv tool upgrade boardmail
```

To replace a Git checkout, local wheel or old version constraint with a registry installation that allows future upgrades, choose the command matching your installation:

```sh
uv tool install --force 'boardmail>=0.15.0'
# With MCP support:
uv tool install --force 'boardmail[mcp]>=0.15.0'
```

Check `uv tool list`, `boardmail --help` and, when installed, `boardmail-mcp --help`. Inspect the retained inbox with `boardmail --db /path/to/inbox.sqlite3 status`, substituting its actual path, then restart the collector or MCP server. This release needs no `init` for an existing inbox: the first `collect` or `check` adds the index. Saved messages, read/reply marks, subscriptions, pauses, continuation state and checkpoints remain. Upgrade every collector sharing the inbox before resuming.

For a package rollback, stop all writers again, preserve the current state and reinstall `boardmail==0.14.2` (or `boardmail[mcp]==0.14.2`) with `uv tool install --force`. Version 0.14.2 reads and writes the same database, index included, so a rollback needs no database change. It brings back the request timing of 0.14.2 and its acceptance of an answer that was cut short. Do not use `init` as a repair. If restoring a pre-upgrade snapshot, keep the current files too: later arrivals, marks and checkpoints require separate reconciliation. See [optional-ledger recovery](docs/reference.md#recover-a-damaged-optional-ledger).

## 0.14.2, 2026-10-03

`mark replied` now validates its HTTP(S) reply URL with the same rules as `reply confirm`. Malformed or out-of-range ports, broken brackets, credentials (including empty credentials) and control characters return `reply_ref_required` before changing the saved message or reply journal. Valid reply URLs keep their existing behavior. Both commands record local evidence; neither publishes a reply to a provider.

Existing invalid marks are retained. After independently verifying the published reply and its destination, use `mark replied` again with the valid URL, then confirm the unknown attempt with its original key and exact saved-body readback. A mark alone does not resolve an unknown attempt or authorize another publication. See [reply recovery](docs/replies.md#resume-after-a-crash-or-unclear-response).

This patch changes no SQLite schema and requires no `init` for an existing inbox. Saved messages, read/reply marks, subscriptions, pauses, continuation state and checkpoints remain. It changes no MCP timeout, SDK or host lifecycle behavior; the existing [MCP compatibility and lifecycle limits](docs/mcp.md#loopback-http-and-protocol-support) still apply.

### Updating and rollback

Stop collectors and long-lived MCP servers before replacing their environment. With writers stopped, back up the config and SQLite database, and preserve any optional consumer ledger and delivery checkpoint. For an unpinned registry installation:

```sh
uv tool upgrade boardmail
```

To replace a Git checkout, local wheel or old version constraint with a registry installation that allows future upgrades, choose the command matching your installation:

```sh
uv tool install --force 'boardmail>=0.14.2'
# With MCP support:
uv tool install --force 'boardmail[mcp]>=0.14.2'
```

Check `uv tool list`, `boardmail --help` and, when installed, `boardmail-mcp --help`. Inspect the retained inbox with `boardmail --db /path/to/inbox.sqlite3 status`, substituting its actual path, then restart the collector or MCP server. Upgrade every collector sharing the inbox before resuming; this patch retains the 0.14.1 continuation behavior.

For a package rollback, stop all writers again, preserve the current state and reinstall `boardmail==0.14.1` (or `boardmail[mcp]==0.14.1`) with `uv tool install --force`. Version 0.14.1 reads the same database format but restores the weaker reply-mark URL validation. Do not use `init` as a repair. If restoring a pre-upgrade snapshot, keep the current files too: later arrivals, marks and checkpoints require separate reconciliation. See [optional-ledger recovery](docs/reference.md#recover-a-damaged-optional-ledger).

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
