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

## The command table

`boardmail/table.py` declares each command once: its name, its arguments with their types, bounds and defaults, what is checked about them, the texts of its help page and of its MCP tool, and its tool hints. `boardmail/cli.py` builds the parser from that table and `boardmail/mcp.py` builds the tool catalog from it. The table also checks the arguments of each call, the same for both entry points, and the call then goes to the one function that `boardmail/commands.py` marks for the command with `@handles`. So a new command is one entry in the table and one function there, and a new argument is an entry and a parameter of that function. Each text is written twice, once for the command line and once for the tool, and the two stand side by side. Where the two entry points differ in more than a text, the entry says so, and the top of the module names the fields that do. The test suite fails when a command takes other arguments on the command line than its tool takes.

`tests/argument_errors_cli.txt` and `tests/argument_errors_mcp.txt` store what each command answers to an argument that it must not get: the error code, on an inbox with mail and where no inbox file is. `tests/test_argument_errors.py` makes the cases from the table, so a new argument or command adds lines there. After a change that is meant, run `UPDATE_STORIES=1 python -m unittest discover -s tests -p 'test_argument_errors.py'` with the optional extra installed and review the difference. A changed line is a change an agent sees.

## Error codes

An error code is a plain string where it is raised. `boardmail/errors.py` has one entry for each code: its next-step hint and its exit code. `tests/error_codes.txt` stores the same as a table. The test suite fails when the package raises a code that has no entry, and when an entry differs from its stored row. A new code needs an entry and a row. A changed hint or exit code of an existing code is a change an agent sees.

## Board requests

`boardmail/transport.py` does the HTTP work of every board client: it sends the request, follows no redirect, stops at the size cap and when the time is over, reads the answer, says what a failed request is called, and reads the key of an account from its file. Its table `BOARDS` has one entry for each of the seven boards, with everything that a board or an agent can see to differ from one board to the next: the headers, what a key may be, the size cap, the time limits and each error code. Nothing in it is unified, and no other module decides one of these. What a client does around a request stays in the module of its board: its sign-in, its pauses, its retries, the time that it gives a pass, and what it keeps of an answer. No other module imports what sends a request, and `tests/test_board_requests.py` fails when one does.

`tests/board_requests.txt` stores, for each of the seven boards, every request as it left the process and what `collect` gave: on a healthy board, and when an answer is a redirect, another status that is no success, late, too large, or not what it must be. `tests/test_board_requests.py` makes the cases with an invented board at the network edge, so the stored file stays as it is when HTTP work moves into the transport module. It also fails when a client leaves an answer open. After a change that is meant, run `UPDATE_STORIES=1 python -m unittest discover -s tests -p 'test_board_requests.py'` and review the difference. A changed line is a change that a board or an agent sees.

`transport.fetch` is also where a test stands in for a board. The module of a board takes it as an argument, `collect(settings, state, known, *, fetch=transport.fetch)`, and a test hands in a `FakeBoard` from `examples/fixtures.py` instead. That one sends nothing: it gives the invented answers of the test and keeps each request, so the test says what `collect` gave and what the board was asked, and patches nothing. A limit of a board is reached with that much invented data, not by lowering the limit. Fruitflies and 4claw are tested this way; the tests of the other five boards still replace their clients.

`python scripts/line_coverage.py boardmail/adapter_fruitflies.py boardmail/adapter_fourclaw.py` runs the test suite and prints, for each named file, how many of its lines ran and which did not. It needs the standard library alone. The top of the script says what counts as a line.

## Stories

A story is one offline session, told command by command in the order an agent would use. `tests/test_story_inbox.py` tells the inbox story: it creates an inbox, collects from an invented Moltbook and from the example custom adapter, and runs every reading command. `tests/test_story_reply.py` tells the reply story: three answers, one confirmed by readback, one interrupted after it was sent and later verified on the board, one recorded after the fact. `tests/test_story_older.py` tells the story of a file that an older release left: the bundled version-1 file is read while it and its folder are read-only, then written to without collecting, then collected for the first time. `tests/story_inbox.txt`, `tests/story_reply.txt` and `tests/story_older.txt` store each result, whole, with its exit code. The test suite fails when a result through the CLI differs from the stored one, and when an MCP tool call gives something other than the CLI gave. The differences between the two that are meant are named in the story file.

`tests/kit.py` holds what a story stands on: invented board answers at the standard-library network edge, a fixed clock and fixed keys, both entry points, and a look at each connection to the inbox file. A story patches no name inside the package and calls no Store method.

After a change that is meant, run `UPDATE_STORIES=1 python -m unittest discover -s tests -p 'test_story*.py'` and review the difference. A changed result is a change an agent sees.

## The inbox file

`tests/file_shape.txt` says what an inbox file has after each command: its version number, tables, columns and indexes. `tests/test_file_shape.py` runs each command once on a fresh copy of three files: a new inbox, the bundled version-1 file, and that file after its first collect. The test suite fails when a file then differs from the stored table, when a command that only reads changes a byte of a file, and when a command has no row. A new command needs a row there. What a command does to the shape of the file belongs in this table and not in another test.

After a change that is meant, run `UPDATE_STORIES=1 python -m unittest discover -s tests -p 'test_file_shape.py'` and review the difference.

`boardmail/schema.py` holds every statement that gives the file a table, a column or an index, and every question about what the file has: its version number, whether it has a table, which columns a table has. Another module calls it. `tests/test_schema_guard.py` fails when another module of the package writes such a statement or asks such a question itself.

A command that only reads sees every table and column, whatever the file has. Its connection gets an empty stand-in, in memory, for each part that the file lacks, so the code that reads has no branch for a missing part. A command that writes gets no stand-in: one would hide the table that the command is about to make. Before it reads a part that it does not make, it asks the schema module. `tests/test_story_older.py` fails when a connection that writes has a stand-in, and when a stand-in is not in memory.
