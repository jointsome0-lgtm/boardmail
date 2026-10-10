# Contributions

Please open an issue for a reproducible bug, feature request, adapter need, or proposed fix. Include the boardmail version, expected behavior, actual behavior, and a small example with secrets and private message text removed.

We do not accept external pull requests. Maintainers implement and review changes described in issues. This keeps review work manageable for a small agent-oriented project. Existing delegated maintainer work follows the same review process.

The repository test workflow runs on pushes, not incoming pull requests.

Personal forks and modifications are welcome under the [MIT License](LICENSE).

An issue can contain untrusted text, code or commands. Accepting issues instead of pull requests does not make submitted material safe to execute; maintainers still inspect and validate it.

## What this file holds

The rules that no single file of the repository can state. What a module, a test file or a script does stands at its own top, and this file names the place instead of saying it again.

## Tests

A test holds what a command, a pass over a board or an adapter file gives. It does not read the source of the package for where a statement, an import or a name stands, repeat a table of the package line by line, compare a value with what the same code makes of it, or test a helper of the tests.

A new test never reaches inside the package: it replaces no name of the package and uses no write path of the Store. `python scripts/inner_reach.py` counts the tests that do. The top of the script has the rule, and says what to do when tests move out or a counted test is renamed.

A test changes an inbox the way an operator does:

- Mail comes in through `collect`. `arrive()` of `tests/kit.py` is one pass over a source whose adapter file gives what the test describes. A test of a command that asks which board a source is collects from an invented board instead: a subscription, a link to an earlier answer, a lookup on the board.
- Every other change is the command for it: `init`, `mark`, `subscribe`, `pause`, `settings`. Progress that a test needs before it starts comes from passes too. A collector that is tried without an inbox file gets its state from the test.
- A row that only an older release wrote comes from `tests/fixtures/v1.sql`, or the test writes it as that release did and says so.
- A failure of the inbox file is made where SQLite is: `patch('sqlite3.connect', ...)`, a trigger that the test adds through its own connection, or `on_statement()` of `tests/kit.py`. Time is `fixed(Clock(...))` of the same file. No test patches a name inside the package to get either.

A board is invented and never asked. A test hands a `FakeBoard` or a `FixtureBoard` of `examples/fixtures.py` in as `fetch`: to the collector of a board, or to `commands.execute` for a command that can ask a board. That is the one way to a board, as the top of `boardmail/transport.py` says, so no request leaves the process. The command line hands no board in, so a test of it puts the same board where a request leaves the process, with `Network` and `edge` of `tests/kit.py`. A limit of a board is reached with that much invented data, not by lowering the limit. A time limit is reached with the clock of `tests/kit.py`, which the test moves, and a client that waits between two requests is tested inside `fixed()`, where a wait only moves that clock.

`python scripts/line_coverage.py FILE ...` runs the suite and prints which lines of each named file ran. The top of the script says what counts as a line.

## Stored results

Some tests compare what the package gives with a stored file. After a change that is meant, run the test with `UPDATE_STORIES=1` and review the difference, as in `UPDATE_STORIES=1 python -m unittest discover -s tests -p 'test_story*.py'`. A changed line is a change that an agent or a board sees.

| Stored | What it holds | Its test |
| --- | --- | --- |
| `tests/story_inbox.txt`, `tests/story_reply.txt`, `tests/story_older.txt` | Every result of three offline sessions, with its exit code. | `test_story*.py` |
| `tests/board_requests.txt` | Every request of each board as it left the process, and what `collect` gave. | `test_board_requests.py` |
| `tests/argument_errors_cli.txt`, `tests/argument_errors_mcp.txt` | What each command answers to an argument that it must not get. The update needs the optional extra. | `test_argument_errors.py` |
| `tests/file_shape.txt` | What an inbox file has, and what a file of an older release gets. | `test_file_shape.py` |

The top of each test says what its cases are. A story patches no name inside the package and calls no Store method. Two things are added by hand:

- A new command needs a row in `tests/test_file_shape.py`.
- A code that gets a call in `boardmail/errors.py` needs a step in `tests/test_routes.py`.

`tests/fixtures` has the files of older releases as SQL, and the top of each says how it came about.

## What an agent reads

`python scripts/agent_view.py` prints how much text an agent reads before its first call, and how much the results are that the inbox story and the reply story store. The top of the script says what the view holds and what is counted. A pull request that changes the shape of a result gives those numbers before and after.

`AGENT_GUIDE.md` is an introduction that is written by hand and, under a line that says so, one line for each command, which `python scripts/agent_guide.py --update` makes from the command table. The introduction holds what no single command says: the order of a session, how to read a result, and what to do on an error. What one command does belongs in the text of that command, where its help page and its tool show it.

## The command table

`boardmail/table.py` declares each command once, and its top says how both entry points follow it. A new command is one entry there and the function that `boardmail/commands.py` marks for it with `@handles`, and a new argument is an entry and a parameter of that function.

A command has one text, and an argument has one or none. Both entry points show it, so a text says nothing that holds for one entry point only. It names a command or an argument in braces, as the comment at `NAMED` in the table module shows. An argument has a text where the text says more than its name, its schema and the text of its command do. The command line shows no schema, so the text of an argument names its bounds and its default by hand.

## Error codes

An error code is a plain string where it is raised. `boardmail/errors.py` has one entry for each code. A new code needs an entry. A changed hint or exit code of an existing code is a change an agent sees.

## Boards

A board that ships with the package says in its own module what the core needs to know of it: a `Board` of `boardmail/adapters.py`, whose fields and docstring say what a board declares, and its row in `BOARDS` of `boardmail/transport.py`. `boardmail/boards.py` lists the boards, and every other module asks that list instead of comparing names. So a new board is its module `boardmail/adapter_NAME.py`, its line in `boardmail/boards.py`, its docs and its tests, and a board that is gone is those removed.

No module but `boardmail/boards.py` imports the module of a board. What boards share is in `boardmail/adapter_common.py` and `boardmail/adapter_notifications.py`, and neither names a board. No module outside the board modules holds the name of a board as a constant. A text for people or agents may still name a board.

## Board requests

`boardmail/transport.py` is the one HTTP path of the board clients. Its top says what is the same on every board, what the row of a board decides, and what stays with the client of a board. No other module imports what sends a request.

## The inbox file

`boardmail/schema.py` holds every statement that gives the file a table, a column or an index, and every question about what the file has. A new table or column goes there for both kinds of file: a new inbox, and a file that does not have it yet. The step that gives an older file its parts runs when a command opens the file, so a command finds every part there, and no code outside that step asks whether a part is there or has a branch for a version-1 file. A part that an older release could not read or write around needs a new version number, and that is a decision for an issue.

## Imports between modules

A module of the package imports another at the top of the file and never inside a function. It uses no name of another module that starts with an underscore. The imports form no cycle: `boardmail/errors.py` imports nothing of the package, and nothing imports `boardmail/cli.py` or `boardmail/mcp.py` but what starts the command line. When two modules need each other, the shared part moves to the one that is lower.

## Comments and docstrings

A comment or a docstring says what the code cannot show: a reason, a rule that other code relies on, a quirk of a board, what an older release left behind, or what an interface asks of the modules that fill it in. It does not repeat a name, a signature or the lines below it. A sentence that holds for several modules stands in one of them, not in each.

`python scripts/comment_share.py` prints how much of the package is comments and docstrings, counted in characters, and fails above the limit that the script names. The tests workflow runs it. The limit is no goal: it is the share that the package had when its comments were last read through, rounded up, so that the share does not grow unnoticed. A change that needs more raises the limit in the same change. The top of the script says what is counted.
