# Changelog

## Unreleased

A folder that Boardmail has no right to enter answers alike on every Python version. Two answers depended on the version, because Python 3.14 looks at a file in such a folder in another way than 3.11, 3.12 and 3.13 do.

- An inbox file in such a folder is `local_state_error` with the reason `permission_denied`. On Python 3.14 a command that reads the inbox answered `database_missing` and named `init` as the next step, though the file was there, and `init` then answered `local_state_error`. A missing inbox file is `database_missing` as before.
- Links that lead in a circle at the path of the inbox are `local_state_error` as well, on every Python version. The answer was `database_missing` with `init` as the next step, and `init` answered `database_exists` with `status` as the next step, so the two named each other.
- A folder `adapters` of that kind is a folder without the file: a source that has its file there is `invalid_config`, as with a missing file. On Python 3.11, 3.12 and 3.13 the answer was `local_state_error` with the next step `inspect_database_do_not_delete`, which sent the operator to an inbox file that had no fault.
- `ADAPTERS.md` says that a missing folder file makes the whole config `invalid_config`, and names `adapter_load_failed`, the code of a file that cannot be loaded. No behavior changes with either.
- The upgrade note of 0.17.0 below says one thing more exactly, and no behavior changes with it. A source that is not collected any more turns `stale` where its last pass was ok. Where that pass failed, the source stays `error`.

## 0.17.0, 2026-10-10

4claw and Fruitflies are no boards of the package any more. It has five: Postingboard, The Colony, Moltbook, ClawdChat and Botnet. For the five boards that stay and for a source of an adapter file nothing changes: commands, MCP tools, results, error codes and the inbox file are as in 0.16.1. Somebody whose config has no source of the two updates as before and has nothing else to do. Somebody whose config has one reads the upgrade note below before the update: the source is not collected after it, and a config that has it under the name of its board is `invalid_config`.

- `fourclaw` and `fruitflies` are gone from the package, with their guides, their examples and the setting `watched_threads`. Nobody checked the two against the boards themselves: the live pass of 0.16.0 left both out. 0.16.1 is the last release that has them.
- In a config either name is a name like any other. A source of that name that names no `adapter` is `invalid_config`, and with it the whole config, unless the folder `adapters` has a file of that name. A source that names one of the two in `adapter` names a file that is not there. Its config is read, and its pass fails: with `adapter_mismatch` where the inbox holds the source under the adapter of the package, as it does after a pass of that source, and with `adapter_load_failed` otherwise. The other sources are collected.
- The mail that an inbox holds of the two stays, and `status`, `list`, `show` and the other local commands read it without a config that names its source. One thing is read differently: where the inbox holds the opening post of a thread as the parent of a 4claw message, the brief names that post as the parent, `same_as_root`. It said `unknown`. And `status` gives such a source the `coverage` of a source of an adapter file.
- `subscribe` names the four boards that take a subscription: Postingboard, Colony, Moltbook and ClawdChat. Nothing changes for them, nor for Botnet.

### Upgrade note

For a config that has a source of 4claw or Fruitflies:

1. To go on with the board, stay on 0.16.1: `uv tool install --force 'boardmail==0.16.1'`, with `[mcp]` where that is installed.
2. To update, first pause the source, `boardmail pause SOURCE`, and then take it out of the config. A source that the inbox knows and that is not collected any more turns `stale`, or stays `error` where its last pass failed, and `status` is not `fresh` then; a paused one does not. A config has at least one source: where these were its only ones, a config without them is `invalid_config`, so leave the config out and name the inbox with `--db`.
3. Then update. The mail of the source is in the inbox as before.

An adapter file can read a board that the package does not have; [ADAPTERS.md](ADAPTERS.md) says how. Give its source another name than the one that the inbox knows, or a new inbox: a source does not change its adapter, and a pass that tries is `adapter_mismatch`.

For a rollback, reinstall `boardmail==0.16.1`. It reads and writes the same file. Put the source back into the config and `boardmail resume SOURCE`.

For every other config: `uv tool upgrade boardmail`, or `uv tool install --force 'boardmail>=0.17.0'`, with `[mcp]` where that is installed. The inbox file is the same, and no command has to be run first.

### For Python callers only

`boardmail.adapter_fourclaw` and `boardmail.adapter_fruitflies` are gone. `transport.Board` has no `kind` and no `budget`: the answer of every board is JSON, and `transport.fetch` needs `left`, the seconds that the request may take. `adapters.Board` has no `parent_is_membership`.

## 0.16.1, 2026-10-10

An adapter file has a place from which it is picked up. Nothing else changes: commands, MCP tools, results, error codes and the inbox file are as in 0.16.0. Somebody who has no adapter file has nothing to do. Somebody who comes from 0.15.1 or older reads the upgrade note of 0.16.0 below.

- An adapter file has a place: the folder `adapters` beside the config. A source that is no board of the package and names no `adapter` is collected by `adapters/NAME.py`, where `NAME` is the name of the source. Such a source was `invalid_config`, and is so still where the file is missing. A file runs only for a source that the config names. A source that names its `adapter` is as before.
- The section of 0.16.0 below says four things more exactly, and no behavior changes with them. A message leaves out eight of its fields where they hold nothing, and not every such field: `addressing` is `null` as before where no recipient is confirmed. A result of a reply command leaves out six of its fields in that way, and `history_complete` is in every result as before. The stored stories of the tests gained an eighth page, which names a source that failed, so `python scripts/agent_view.py` counts one source row of 468 characters in this tree.

### Updating and rollback

Update as the section of 0.16.0 says, with `0.16.1` as the version: `uv tool upgrade boardmail`, or `uv tool install --force 'boardmail>=0.16.1'`, with `[mcp]` where that is installed. Coming from 0.16.0 there is nothing else to do: the inbox file is the same, and no command has to be run first.

For a rollback, reinstall `boardmail==0.16.0`. It reads and writes the same file. A source that has its file in `adapters` and names no `adapter` is `invalid_config` in 0.16.0, and with it the whole config: name the file in `adapter` before going back.

## 0.16.0, 2026-10-09

The release of phase 2 of the core cleanup. What an agent reads before its first call and what a call gives back are smaller. An error names its next step as a call that can be made, a request that failed has one code on every board, and each command says the same on the command line and through MCP. Results, the hints of errors and four error codes change, all in this one release, and the upgrade note says what to do about each. No command, MCP tool or argument is added, renamed or removed. Saved messages, read and reply marks, subscriptions, pauses, tags, reply attempts, continuation state and checkpoints are kept.

| What an agent reads before its first call, in characters | 0.15.1 | 0.16.0 |
| --- | --- | --- |
| The tool list of the MCP server as JSON | 114,367 | 25,355 |
| In it, the output schemas | 89,934 | none |
| In it, the tool descriptions | 10,314 | 10,435 |
| In it, the input schemas | 9,379 | 10,596 |
| The instructions of the MCP server | 1,342 | 1,493 |
| The help text of the command line, 29 pages | 18,070 | 16,826 |
| The agent guide | 12,591 | 5,781 |

What a call gives back, in the two stored stories of the tests: the fifteen messages on the pages of `check`, `list` and `wait` are 10,111 characters and were 15,832, and the brief context of ten of them is 2,644 and was 6,632. The seven pages of those stories named a source 14 times, in 5,604 characters, and the same pages name none now. The stories gained an eighth page, on which a source that failed is named: one row of 468 characters. `python scripts/agent_view.py` counts all of this in the tree of each release, and the guide is the file `AGENT_GUIDE.md`.

### Upgrade note

What somebody who comes from 0.15.1 has to do. Each step is told in full under "What changes".

**A caller that reads pages and messages**

- Read on while `more` is true, with `after` set to `next_after`. A page of `check`, `list` and `wait` holds 20 arrivals where the call names no limit, and held 100. A caller that wants 100 at once passes `--limit 100`, or `limit: 100` to the MCP tool. The `replay` arguments of a thread summary name no `limit` any more.
- Take `sources: []` on a page to say that every source is `ok`, and ask `status` for the health of every source. A page names a source only when its status is not `ok`; it named each.
- Read these fields of a message with a default, because each is left out where it holds nothing: `null` for `parent_id`, `provider_seq`, `read_at`, `replied_at`, `reply_ref` and `discovery`, `false` for `needs_reply`, and `[]` for `tags`. An excerpt of a brief has `truncated` only as `true`.
- Call `context SOURCE ID` for the addresses and the `reply_ref` that the brief context had; it has them where the source supports that lookup. In a brief, the root, the parent and an earlier message of the exchange have no `url` and no `fetched_at`, and have `thread_id` and `title` only where those differ from the message's own. `previous_exchange` has no `reply_ref`, and the brief has no `expand`.
- Read `tool` where a route had `command`. A route is `{"tool": ..., "arguments": ...}` everywhere, and `command` is gone from `replay` and `expand` of a thread summary, from `read` and `show` in the results of the tag commands, from `show` of a reply attempt and from `next` of a page of reply attempts.

**A caller that handles errors**

- Compare `error`, which is the stable name, and not `next_action`: 24 codes have another word there. None of them is renamed, and no exit code changes.
- Read `next` where an error had `recovery` or `identifier_hint`. `recovery` is gone from a `local_state_error` of a reply command, and `identifier_hint` from `message_not_found`. `next` is new: it is a route to the call that is the next step, on an error whose next step is one call.
- Know a request that failed by the code that every board gives it. Four codes are gone. On 4claw, `fourclaw_http_error` is `http_` and the status, or `redirect_refused`; `fourclaw_network_error` is `network_error`, or `source_timeout`; and `fourclaw_invalid_public_page` is `invalid_response`, or `response_too_large`. On Fruitflies, `network_timeout` is `source_timeout`, and a redirect that names where to ask instead is `redirect_refused`. On ClawdChat and Botnet an answer that came late is `source_timeout`; it was `budget_exhausted`. The row of a source can have one of the four old codes until the next pass of that source.

**A caller of the reply commands**

- Read these fields of a result with a default: `confirmation_basis`, `reply_candidates`, `recovery_guidance`, `remote_verified`, `verification` and `verification_receipt`, and in `reply` the fields `attempted_at`, `confirmed_at`, `reply_ref` and `readback_sha256`. Each is absent where it was null, false or an empty list. `publication_performed` is gone: it was false in every result.

**An MCP client**

- Check a result against no declared schema: no tool declares an output schema. A result is unchanged, as text and as `structuredContent`.

**Whoever runs a collector or an MCP server**

- Run the first command after the update where the inbox file can be written: `boardmail status` is enough. It brings the file of an older release up to date in one transaction that keeps every row, also when it is a command that only reads. Where the file cannot be written, that command returns `local_state_error` and leaves the file as it was.
- Look at each key file. A key is up to 4,096 characters, each printable ASCII and none a space, and white space around it is dropped. A file that holds anything else is `credentials_unavailable`, and the board is not asked. A key file that worked keeps working unless what it holds between the white space around it is longer than 4,096 characters, or has in it a space, a line break or a character that is not printable ASCII.
- Every board is told one user agent, `boardmail/0.16.0`. Where a proxy or a filter of the installation names one of the four old ones, it has to name this one.
- A notification whose original is gone no longer makes a Botnet source an error, and no longer keeps `backlog_pending: true` on ClawdChat and Botnet where nothing else is left to do. A monitor that alerted on either sees the change. `backlog_pending: true` is no fault and asks for no step.
- Update every collector that shares the inbox, as "Updating and rollback" says.

**A Python caller of the package**

- Pass `limit` to `Store.page()` and `Store.wait()` where a page of 100 is wanted: their default is 20 as well. Change the calls that the entry "For Python callers only" names: `transport.key` and `transport.failure` take no board, `Store.prepare_collection()` is gone, and a record of `boardmail.table` has one `text`. What an adapter file imports is unchanged.

**Nothing to do**

An error names the argument whose value was refused, in `argument`. One address of a board host has 3 seconds to take a connection, and one rule says when an answer is late. ClawdChat and Botnet ask first for the references that are not known to be gone. Every command has one text for the command line and for MCP, and the agent guide is shorter. `kind` stays, and an adapter file collects as before.

### What changes

**Pages and messages**

- A page of `check`, `list` and `wait` holds 20 arrivals where the call names no limit; it held 100. `more` and `next_after` lead through the rest as before, and a limit from 1 to 500 is taken as before. A caller that relied on 100 passes `--limit 100`, or `limit: 100` to the MCP tool.
- The `replay` arguments of a thread summary name no `limit`; they named 500. A replay page has the default size. While it has `more: true`, read on with the same arguments and `after` set to its `next_after`.
- A page of `check`, `list` and `wait` names a source only when its status is not `ok`: `stale`, `error`, `unknown` or `paused`. It named every source. `sources: []` says that every source is `ok`. A caller that read the health of every source from a page asks `status`. `backlog_pending` puts no source on a page: where a board holds more than one pass reads, it is the usual state of a healthy source, because a pass counts as finished only when a scan that goes round is at its end. `status` and `collect` report it as before.
- A message in a result leaves out eight of its fields where they hold nothing. `parent_id`, `provider_seq`, `read_at`, `replied_at`, `reply_ref` and `discovery` were written as `null`, `needs_reply` as `false` and `tags` as `[]`; each is now absent where it had that value, and is as it was where it holds something. This is so wherever a result carries a message: on a page of `check`, `list` and `wait`, in `show`, `mark`, `context` and `expand`, and in the reply commands. An excerpt of a brief has `truncated` only as `true`; it had `truncated: false` where it was not cut. A caller reads these fields with a default: `null` for `parent_id`, `provider_seq`, `read_at`, `replied_at`, `reply_ref` and `discovery`, `false` for `needs_reply` and `truncated`, `[]` for `tags`. Only a message and an excerpt change: a page, a source row, a thread summary, a reply attempt and `previous_exchange` have their fields as before, also where one is `null`, `false` or empty. The other fields of a message are as before as well: `addressing` is `null` where no recipient is confirmed.
- The brief context of a message says each thing once. Its root, its parent and an earlier message of the exchange have no `url` and no `fetched_at`, and have `thread_id` and `title` only where those differ from the message's own. `previous_exchange` has no `reply_ref`, and the brief has no `expand`. `context SOURCE ID`, the call that `expand` named, has the addresses and the `reply_ref` where the source supports that lookup. No result has the time at which a cached excerpt was fetched.

**Errors and the next step**

- The hint of an error names a step that can work. 19 codes said `retry_collect` although another pass gives the same outcome. `original_deleted`, `original_unavailable`, `original_incomplete`, `thread_deleted`, `thread_missing` and `hidden_by_provider` now say `continue_without_the_original`. `reply_deleted`, `reply_missing`, `reply_not_visible`, `reply_incomplete`, `reply_author_mismatch`, `reply_identity_mismatch`, `reply_target_mismatch`, `reply_thread_mismatch`, `reply_provider_not_verified` and `reply_provider_status_unknown` say `reconcile_publication_before_retry`. `redirect_refused`, `response_too_large` and `invalid_request` say `report_to_the_operator`. `retry_collect` stays for a pass that could not finish. Five codes said `check_command_help_and_returned_message_ids`, which a caller on MCP cannot follow: `invalid_arguments` now says `fix_the_arguments`, `invalid_message_id` says `use_the_exact_id_of_a_returned_message`, `message_not_found` says `use_the_source_and_id_of_a_listed_message`, `invalid_mark` says `give_ref_only_with_action_replied`, and `reply_ref_required` says `supply_the_url_of_the_published_reply_as_ref`. No code is renamed and no exit code changes. A caller that compared `next_action` with one of the old words compares it with the new one, or with `error`, which is the stable name.
- An error names the argument whose value was refused, in a new field `argument`: `{"event": "error", "error": "invalid_arguments", "argument": "limit", ...}`. The name is the one the MCP tool has for the argument. The field is absent where the call has a word that is no argument of the command, where it breaks a rule between two arguments, and where the error says that something is not there or does not match, as `message_not_found` does. The command line leaves it out as well for an argument by position that is left out, for an option without its value, and for an option that is given twice. Which code a wrong call is answered with is unchanged.
- A call that a result names is written in one shape, a route: `{"tool": "boardmail_reply_show", "arguments": {"source": "SOURCE", "id": "ID"}}`. `tool` is the MCP tool and `arguments` are its arguments. On the command line the tool is the command of the same name, `reply show` here. The field `command` is gone from every route. `replay` and `expand` of a thread summary had `command` and no `tool`, and now have `tool`. `read` and `show` in the results of the tag commands, `show` of a reply attempt and `next` of a page of reply attempts had both, and keep `tool`. A caller that read `command` reads `tool`. The arguments of a route are unchanged.
- An error names its next step as a call, in a new field `next`, where the step is one call. It is a route, and the call can be made as it is written: `init` for `database_missing`; `status` for `database_exists`, `source_not_found` and `source_paused`; `settings` with `reset: true` for `invalid_settings`; `list` for the source of the call for `message_not_found`; and `reply_show` for the source and id of the call for `reply_already_recorded`, `reply_already_started`, `reply_body_conflict`, `reply_candidate_limit`, `reply_key_mismatch`, `reply_not_prepared`, `reply_not_started`, `reply_readback_mismatch` and `reply_reference_conflict`. Two fields are gone, and `next` says what they said. `recovery` is gone from a `local_state_error` of a reply command: the read of the journal that it named is in `next`, without `command` and `read_only`, and the error has `send_allowed: false` as before. `identifier_hint` is gone from `message_not_found`: its `next` is the list in which the ids of the source are. `next_action` stays on every error, and no code or exit code changes.

**Reply commands**

- A result of a reply command with `event: "reply_attempt"` leaves out six of its fields where they hold nothing. `confirmation_basis`, `reply_candidates`, `recovery_guidance`, `remote_verified`, `verification` and `verification_receipt` are absent where they were null, false or an empty list. The `reply` of a result has `attempted_at` from `reply begin` on, and `confirmed_at`, `reply_ref` and `readback_sha256` once it is confirmed; they were null before that. Absent means none, not unknown: without `reply_candidates` there is no unknown attempt with a saved candidate URL, and without `remote_verified` the call did not verify the reply through its provider. `publication_performed` is gone from every result, from `reply list` too: it was false in each one, because no reply command publishes. `event`, `message`, `reply`, `changed`, `send_allowed`, `next_action`, `collection_performed` and `history_complete` are in every such result as before, `changed` and `send_allowed` also where they are false, and `reply` is null where no reply is saved. An error envelope, with `event: "error"`, is as it was and has none of the ten fields, whatever the journal holds. No field changes its name or its meaning. A caller that reads one of the fields named here reads it with a default. For one invented message the result of `reply prepare` is 730 characters; it was 1,102.

**Tools, help pages and the guide**

- No MCP tool declares an output schema. Each of the 26 tools declared the same one, the union of what any result can hold, so it could not tell the result of one tool from the result of another. It was four fifths of the tool list: 89,934 of its 114,367 characters. With the other changes of this release the tool list is 25,355 characters. A result is unchanged, as text and as `structuredContent`. A client that checked a result against the declared schema has none to check against.
- Every command says the same on the command line and through MCP. Each of the 26 has one text, which is its help page and the description of its tool, and each of its arguments has one or none. The command table has no field for a second text. A name is written as the reader uses it: `--require-fresh` on the help page is `require_fresh` in the tool. No name, argument or result changes. The help page of a command loses its examples and its second wording; a usage line shows how the command is typed. The help text of the command line goes from 18,070 to 16,826 characters. Tool descriptions are 10,435 characters and were 10,314. An argument that only the help page described now has its description in the schema of the tool as well, so the input schemas go from 9,379 to 10,596 characters. There are 33 such arguments: `require_fresh` of `status`, `reset` of `settings`, `thread` of `unsubscribe`, `source` of `pause`, `resume`, `subscribe`, `unsubscribe`, `subscriptions`, `tag add` and `tag remove`, of the reply tools `id` of five, `key` of three, `ref` of two, `limit`, `replace_key` and `readback_body`, and of the reading tools `limit` of `check`, `list`, `wait` and `expand`, `through`, `thread` and `untagged` of `list`, `action` of `mark`, and `local` of `context` and `expand`. Fifteen arguments that the help page described have no description now, because the name says what each is: `source` of the five reply commands that take a message and of `show`, `mark`, `context`, `expand` and `list`, `id` of `show`, `mark` and `context`, `thread` of `expand`, and `unread` of `list`. `scope` and `context` of `check`, `list` and `wait` no longer say what their values mean; they name `settings`, which says it. `scope` and `context` of `boardmail_settings` said that they override a saved preference once, which is what they do in `check`, `list` and `wait`; they now say that they save it.
- The agent guide is 5,781 characters; it was 12,591. It is an introduction and one line for each command. The introduction says what no single command says: the order of a session, how to read a result, and what to do on an error. The line of a command is the first sentence of its help page, made from the command table. What the guide said about single commands in words of its own is gone: the help page of the command and the description of its tool say it, and the reference has the rest.

**Requests to the boards**

- A request to a board that failed has one code, whichever of the seven boards it was sent to. `source_timeout`: the answer came after the time of its request. `budget_exhausted`: nothing was sent, because the pass or the command had no time or no requests left. `network_error`: the board was not reached, stayed silent for too long, did not answer in HTTP, or its answer broke off. `response_too_large`: the answer is over the size cap of the board. `invalid_response`: the answer cannot be read as what it must be. `http_` and the status, as `http_503`: the answer has a status that is no success. `redirect_refused`: the answer is a redirect that names where to ask instead. No pass and no command gives four codes any more: `fourclaw_http_error`, `fourclaw_invalid_public_page`, `fourclaw_network_error` and `network_timeout`. A source whose last pass under an older release ended with one of them keeps that code as the `error` of its row, with the hint `retry_collect` as before, until its next pass replaces it; a paused source has no pass until it is resumed. On 4claw, `fourclaw_http_error` is now `http_` and the status, or `redirect_refused` for a redirect that names where to ask instead; `fourclaw_network_error` is now `network_error`, or `source_timeout` for an answer that came late; `fourclaw_invalid_public_page` is now `invalid_response`, or `response_too_large` for an answer over the cap. On Fruitflies, `network_timeout` is now `source_timeout`, and a redirect that names where to ask instead is `redirect_refused`; it had `http_` and its status. On ClawdChat and Botnet an answer that came late is `source_timeout`; it was `budget_exhausted`, which is now only what it says. Their pass goes on from a late answer as before: only the identity check that opens a pass names the code as the error of the source, and what is saved and what the next pass asks for is the same. The element of `context` and `expand` for an original whose answer came late has the new code as its `error` on these two boards, and `budget_exhausted: true` of `expand` is as before. Two hints change with the codes: an answer over the cap on 4claw, and a refused redirect on 4claw and Fruitflies, say `report_to_the_operator`; they said `retry_collect`. Postingboard, The Colony and Moltbook, and every other case on these four, have the code that they had. A caller that compared `error` with one of the four old codes compares it with the new one, and with the old one as well until each source has had a pass.
- One rule says when an answer is late, on every board: when it has not ended before the time of its request is over. An answer that ends at the very moment at which that time is over is late, and so is one that came whole in time and ended after it. Postingboard, The Colony and Moltbook took the first of the two, and ClawdChat, Botnet and Fruitflies took the second; both are `source_timeout` now. 4claw had this rule, but for an answer that began late and was no page: it was `fourclaw_invalid_public_page`, and is late like any other. As before, an answer with a status that is no success has the code of its status whenever it comes, and a redirect `redirect_refused`. The limits of time and size of each board are as they were.
- One address of a board host has 3 seconds to take a connection, or the time of its request where that is less; then the next address of the host is tried. An attempt had as long as the socket of the board may stay silent: 10 seconds on Postingboard, The Colony and Moltbook, 8 on Fruitflies, 5 on 4claw, and 4 on Botnet and ClawdChat. An address that takes no connection now costs a request 3 seconds. An address that needs more than 3 seconds to take a connection is no longer reached, and a host that has no other address then counts as not reached. The time that a connected socket may stay silent is unchanged.
- Every board is told one user agent: `boardmail/` and the version of the package, `boardmail/0.16.0` in this release. The boards were told four, and none was the version: `boardmail/0.2` (Postingboard, The Colony, Moltbook and ClawdChat), `boardmail` (Botnet), `boardmail/1` (4claw) and `boardmail/fruitflies` (Fruitflies). The tests ask no board. One live pass with this release asked five of the seven boards, and the source of each ended `ok`. 4claw and Fruitflies were not part of that pass, so how they treat the new user agent is not known.
- One rule says what the key of an account is, on every board that has a key file: up to 4,096 characters, each printable ASCII and none a space. White space around the key, such as spaces, tabs and line breaks, is dropped, as it was on every board. A file that holds anything else is `credentials_unavailable`, and the board is not asked. On Postingboard, The Colony and Moltbook a key with a space in it, and a key of more than 4,096 characters, was sent; a key of two lines was `invalid_response` on Postingboard and Moltbook, and The Colony sent both lines. ClawdChat and Botnet had this rule for the first 4,097 characters of the file and read no more: a key of more than 4,096 characters that stood after a line break was sent cut to 4,096, and a word that stood after those characters was not seen. Both are refused now, and a key that stands after that many line breaks is taken. A key file that worked keeps working unless what it holds between the white space around it is longer than 4,096 characters, or has in it a space, a line break or a character that is not printable ASCII.

**ClawdChat and Botnet**

- A Botnet notification whose message is gone no longer makes its source an error. Gone is: deleted, hidden, or answered with 403, 404 or 410. With one such notification every pass of the source ended with `original_deleted`, `original_unavailable` or the status as its error, `collect` exited 1, the source had `status: error`, and `last_ok` was not set. The message now counts in `unavailable` and the source stays `ok`, as on ClawdChat. Its reference waits in the queue and is asked for again on a later pass. A message that fails in another way is reported as before.
- On ClawdChat and Botnet a notification whose original is gone is no backlog. Gone is: deleted, hidden, under a deleted post on ClawdChat, or answered with 403, 404 or 410. One such notification kept `backlog_pending: true` for its source from then on, as if more mail were on its way. Now such a reference does not keep a pass from being finished: where nothing else is left to do, `backlog_pending` is false. The reference waits in the queue and is asked for again as before, an original that is there again is delivered, and `status` and `collect` count the original in `unavailable` in each pass that asks for it. A reference that failed in another way, such as a board that was not reached, is backlog as before. So is one that was gone and then fails in another way, until the board answers again that it is gone; a request that got no answer in the time of its phase is no such failure. Where an older release still collects into the same file, its passes count such a reference as backlog as they did.
- On ClawdChat and Botnet a pass asks for the references that wait in this order: first those that are not known to be gone, then those whose original the board has answered as gone. Each of the two groups keeps the order of the queue. The queue was asked in its order alone. So mail whose first request failed, or that a pass found and had no request left for, waited behind every reference that is gone, for as many passes as it took to ask for them all. Now it waits only behind references that may be there as well: the next pass asks for it, unless more of those wait before it than a pass can ask for. The limits of a pass are as they were: its 40 requests, its time, and on ClawdChat eight references that waited. A pass can use more of its requests than before, because a reference that may be there can take more of them than one that is gone. References that are gone get what a pass has left: while more references that may be there wait than a pass can ask for, those that are gone are not asked for.

**The inbox file**

- An inbox file has every table and column from the start. `init` creates it that way, and a file that an older release left gets what it is short of from the first command that opens it, in one transaction that keeps every row. That command can be one that only reads. Before, the first `collect` brought a version-1 file up to date, a command created a table when it first needed one, and a command that only reads never wrote. So the file of an older release has to be writable for the first command of this release: where it is not, the command returns `local_state_error` and leaves the file as it was. An installation that reads its inbox from a place where it cannot write runs one command with write access once after the upgrade, `boardmail status` for example. From then on a command that only reads writes nothing, as before. An older release reads and writes the file after that; this was checked with 0.15.1, 0.14.2 and 0.5.0. A version-1 file is a version-2 file after its first command, and `subscribe` with `--db` alone then takes a source of it without a config; before the first collection it returned `subscription_config_required`. The texts of `subscriptions` and `tags` no longer say that the command migrates nothing.

**Boards and adapter files**

- `kind` stays in every message, and the docs say what it is: the word of the board or the adapter for the message, where `addressing` is what could be confirmed about its recipient. The docs called `kind` legacy, as if it were on its way out. It stays because on The Colony, Moltbook and ClawdChat it is the only trace of what the board said where no recipient is confirmed.
- An adapter file collects mail as before, on interface version 1, and gets no more of what a board of the package has: collecting is one interface already, and nobody is known to use an adapter file. `ADAPTERS.md` now lists the codes that the boards of the package report for a request that failed, with the next step that a result names for each, so that a file can report the same failure under the same code. A code that the error catalog does not have gets `retry_collect`, as before. `ADAPTERS.md` is part of the source distribution.

**Python callers**

- For Python callers only. What an adapter file imports, `from boardmail.adapters import Batch`, is unchanged. So are `collect_all`, `from_file` and `BOARDS` of `boardmail.boards`, `execute`, `context` and `expand` of `boardmail.commands`, and `create_server` of `boardmail.mcp`; what they return changes as the entries above say. `Store.page()` and `Store.wait()` take 20 as the default of `limit`, where they took 100. Other names changed with the code behind them, and a call that was written for 0.15.1 fails there with a `TypeError`, an `AttributeError` or an `ImportError`. `transport.key(file)` and `transport.failure(exc)` take no board; they took `(board, file)` and `(board, exc)`. `transport.called` is gone. A row of `transport.BOARDS` is `Board(accept, protocol, kind, cap, silence, budget)`: the eleven fields for the user agent, the key, the moment from which an answer is late and the codes of a failure are gone, because those are the same on every board. `Store.prepare_collection()` is gone, because `Store.connect()` brings a file up to date, and `add`, `add_pause`, `has`, `stand_in`, `upgrade` and `whole` of `boardmail.schema` are gone with it. In `boardmail.table` an `Argument` has `text` where it had `help` and `tool`, and a `Command` has `text` where it had `summary`, `epilog`, `tool` and `description`. Helpers of the modules behind the commands lost an argument or gained one, among them `commands.local_state_result`, `reader.excerpt` and the functions that took `writing`.

### Updating and rollback

Stop collectors and long-lived MCP servers before replacing their environment. With writers stopped, back up the config and SQLite database, and preserve any optional consumer ledger and delivery checkpoint. For an unpinned registry installation:

```sh
uv tool upgrade boardmail
```

To replace a Git checkout, local wheel or old version constraint with a registry installation that allows future upgrades, choose the command matching your installation:

```sh
uv tool install --force 'boardmail>=0.16.0'
# With MCP support:
uv tool install --force 'boardmail[mcp]>=0.16.0'
```

Check `uv tool list`, `boardmail --help` and, when installed, `boardmail-mcp --help`. Then run `boardmail --db /path/to/inbox.sqlite3 status`, substituting the actual path, where the file can be written. As the first command of this release it brings the file up to date, and it shows the retained inbox. This release needs no `init` for an existing inbox. Saved messages, read and reply marks, subscriptions, pauses, tags, reply attempts, continuation state and checkpoints remain. Then restart the collector or MCP server. Upgrade every collector sharing the inbox before resuming. A collector of an older release that still collects into the file works as its release does: on Botnet, for one, a notification whose message is gone makes its source an error again.

For a package rollback, stop all writers again, preserve the current state and reinstall `boardmail==0.15.1` (or `boardmail[mcp]==0.15.1`) with `uv tool install --force`. Version 0.15.1 reads and writes the file as this release leaves it, so a rollback needs no database change. It brings back the results, the hints and the codes of 0.15.1, and on Botnet a notification whose message is gone makes its source an error again. Do not use `init` as a repair. If restoring a pre-upgrade snapshot, keep the current files too: later arrivals, marks and checkpoints require separate reconciliation. See [optional-ledger recovery](docs/reference.md#recover-a-damaged-optional-ledger).

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
