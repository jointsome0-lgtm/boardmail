# Contributions

Please open an issue for a reproducible bug, feature request, adapter need, or proposed fix. Include the boardmail version, expected behavior, actual behavior, and a small example with secrets and private message text removed.

We do not accept external pull requests. Maintainers implement and review changes described in issues. This keeps review work manageable for a small agent-oriented project. Existing delegated maintainer work follows the same review process.

The repository test workflow runs on pushes, not incoming pull requests.

Personal forks and modifications are welcome under the [MIT License](LICENSE).

An issue can contain untrusted text, code or commands. Accepting issues instead of pull requests does not make submitted material safe to execute; maintainers still inspect and validate it.

## Tests that reach inside the package

`python scripts/inner_reach.py` prints, for each test file, how many tests patch a name inside the package or call a Store write path. The rule is written at the top of that script. `tests/inner_reach.txt` stores the counted tests by name and their number, and the test suite fails when the tests that reach inside are not exactly the stored ones. A new test never reaches inside. After tests move out, run `python scripts/inner_reach.py --update` and review the difference; `--list` shows why each test is counted. When a counted test is renamed or moved, edit its stored line by hand.

## What an agent reads before its first call

`tests/agent_view_cli.txt` and `tests/agent_view_mcp.txt` hold what an agent reads before its first call: the command tree with its help strings, and the MCP server instructions and tool catalog. The test suite fails when either file differs from what the code supplies now. After a change that is meant, install the optional extra, run `python scripts/agent_view.py --update` and review the difference. Without `--update` the script prints how much text that is. The top of the script says what the files hold and what they leave out.

## Error codes

An error code is a plain string where it is raised. `boardmail/errors.py` has one entry for each code: its next-step hint and its exit code. `tests/error_codes.txt` stores the same as a table. The test suite fails when the package raises a code that has no entry, and when an entry differs from its stored row. A new code needs an entry and a row. A changed hint or exit code of an existing code is a change an agent sees.

## Stories

A story is one offline session, told command by command in the order an agent would use. `tests/test_story_inbox.py` tells the inbox story: it creates an inbox, collects from an invented Moltbook and from the example custom adapter, and runs every reading command. `tests/test_story_reply.py` tells the reply story: three answers, one confirmed by readback, one interrupted after it was sent and later verified on the board, one recorded after the fact. `tests/story_inbox.txt` and `tests/story_reply.txt` store each result, whole, with its exit code. The test suite fails when a result through the CLI differs from the stored one, and when an MCP tool call gives something other than the CLI gave. The differences between the two that are meant are named in the story file.

`tests/kit.py` holds what a story stands on: invented board answers at the standard-library network edge, a fixed clock and fixed keys, and both entry points. A story patches no name inside the package and calls no Store method.

After a change that is meant, run `UPDATE_STORIES=1 python -m unittest discover -s tests -p 'test_story*.py'` and review the difference. A changed result is a change an agent sees.

## The inbox file

`tests/file_shape.txt` says what an inbox file has after each command: its version number, tables, columns and indexes. `tests/test_file_shape.py` runs each command once on a fresh copy of three files: a new inbox, the bundled version-1 file, and that file after its first collect. The test suite fails when a file then differs from the stored table, when a command that only reads changes a byte of a file, and when a command has no row. A new command needs a row there. What a command does to the shape of the file belongs in this table and not in another test.

After a change that is meant, run `UPDATE_STORIES=1 python -m unittest discover -s tests -p 'test_file_shape.py'` and review the difference.
