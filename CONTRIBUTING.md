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
