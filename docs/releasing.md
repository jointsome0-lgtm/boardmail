# Releasing Boardmail

The `publish.yml` workflow runs manually. Its default `build-only` mode builds
a source distribution and a wheel, checks their metadata, rebuilds a wheel from
the unpacked source distribution, and tests both installed wheels with MCP support.
Tests, examples and the fixtures of older inbox files come from the source archive, not the
checkout. Pushes and tags do not trigger it. The `publish` job runs
only when a maintainer selects `publish` on `main`.

## One-time owner setup

1. In [repository environments](https://github.com/jointsome0-lgtm/boardmail/settings/environments),
   create `pypi`. Restrict deployment branches to `main` and configure a required
   reviewer to approve each upload. Save these protections before publishing.
2. Sign in to the PyPI account that should own the project. Open
   [account publishing](https://pypi.org/manage/account/publishing/) and add a
   pending GitHub Actions publisher with these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `boardmail` |
   | Owner | `jointsome0-lgtm` |
   | Repository name | `boardmail` |
   | Workflow name | `publish.yml` |
   | Environment name | `pypi` |

The workflow name is the filename, without `.github/workflows/`. A pending
publisher creates the project on its first successful use; it does **not** reserve
the name. If someone else registers it first, stop and resolve the project name.
See [PyPI's pending-publisher guide](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/).
No API token or password belongs in GitHub secrets for this workflow.

## Validate, then publish

1. Merge the reviewed release commit into `main`. For the first release, both
   `pyproject.toml` and `boardmail/__init__.py` must still say `0.4.0`.
2. Open [Build or publish to PyPI](https://github.com/jointsome0-lgtm/boardmail/actions/workflows/publish.yml).
   Choose **Run workflow**, branch `main`, mode `build-only`. Confirm the build
   passes and the publish job is skipped. Inspect the run's commit, metadata
   check, SHA-256 output, and downloadable `boardmail-dist` artifact.
3. Once the owner has approved publication, run the same workflow on the reviewed
   `main` commit with mode `publish`. This run builds and checks fresh artifacts.
   If `main` has moved, validate and review the new commit first.
   Before approving its `pypi` environment, verify its commit and inspect this
   run's artifacts. The upload job retrieves those exact artifacts by ID; it
   does not rebuild them. Keep the run number and hashes as the release receipt.
4. After success, verify [the PyPI release](https://pypi.org/project/boardmail/)
   and test `python -m pip install 'boardmail[mcp]==0.4.0'` in a fresh environment.
   Confirm `boardmail --help` and `boardmail-mcp --help` work.

Only the upload job receives `id-token: write`; it uses the official PyPA action
with its default metadata verification and attestations. The build job has only
repository read permission. Local checks and a green build-only run do not prove
that the PyPI trust configuration works; that is verified by an authorized upload.
See [PyPI's security guidance](https://docs.pypi.org/trusted-publishers/security-model/).

For later releases, update both version locations and use that version in the
installation check. Existing PyPI files cannot be overwritten; inspect a partial
failure before retrying rather than treating duplicate files as success.

## Check standalone artifacts

The source distribution includes the source examples, tests, SQL fixtures of older inbox files,
guides, this artifact checker, the counter of tests that reach inside the package and
the script that writes down what an agent reads before its first call.
The wheel installs the `boardmail` package and its console entry points; it does not
install those ancillary files.

With the declared `setuptools>=77` backend already available, run:

```sh
python -B scripts/check_sdist.py dist/boardmail-VERSION.tar.gz \
  --wheel dist/boardmail-VERSION-py3-none-any.whl --report artifact-check.json
```

The checker inventories both archives, unpacks the sdist outside the checkout,
rebuilds its wheel, and installs each wheel in a disposable environment. It verifies
installed module and entry-point origins, runs both help commands, the offline demo,
and focused custom-adapter, migration and consumer-recovery tests. It uses only the
archive's tests and examples. Installation uses `--no-index`; the checker makes no
provider requests. It preserves neither the temporary inboxes nor the environments.
Use `--rebuilt-wheel-dir PATH` to keep the rebuilt artifact.

The publish workflow first downloads the declared MCP dependency wheels, then passes
their directory with `--mcp-wheels PATH --full-suite` to run every bundled test against
both installations. Without that directory, the checker installs the dependency-free
runtime and runs the focused smoke by default; `--full-suite` can skip optional MCP
tests. Its Python socket guard is cooperative
fixture isolation, not operating-system containment. A local pass does not validate
the complete Python-version matrix or the later integrated release artifacts.
