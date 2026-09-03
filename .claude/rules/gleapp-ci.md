# CI facts that are not obvious from the workflow files

## Seven checks are required, and none of them has a paths filter

`pytest`, `windows-smoke` and `runtime-contract` on 3.10 through 3.14 are required to
merge into `main`, for everyone including admins. Their workflows deliberately carry no
`pull_request` paths filter. A required check that never triggers never reports, and
GitHub then blocks the pull request permanently rather than passing it; measured on a PR
that touched only a workflow file. Path filtering and required status are incompatible,
so filters live only on the advisory jobs.

## The lint job is advisory and has two carve-outs on purpose

`python_lint` runs pylint on the Python files a pull request touches and fails only on
warnings the change introduces (`tools/lint_changed.py`). It runs only when `.py` files
change, so it cannot be required without blocking docs-only pull requests.

It drops `gleapp.py` from the set it hands pylint. Handed a file named after the package,
astroid registers that file as module `gleapp` for the whole run, and every co-linted
`from gleapp.x import y` then resolves into the three-line shim: 58 false
`no-name-in-module` warnings measured on an untouched test file, and
`# pylint: skip-file` does not prevent it. Python itself prefers the package, so runtime
is unaffected. Do not "fix" the carve-out.

It installs the dev extras, pytest and piexif. A new test file has no baseline in the
merge-base comparison, so an import it cannot resolve counts as introduced.

## Windows runs the whole suite, with sqlite3 installed

`windows-smoke` imports every module and then runs the full suite. It installs the
`sqlite3` CLI with chocolatey because `hashstore` shells out to it for NSRL deltas and
Windows does not ship it. Without that, the one test covering that path skipped on
Windows, and a skipped test reports green.

## The frozen build runs on packaging changes, weekly, and on demand

`test_builds` builds on Windows, macOS and Linux, smoke-tests the result headless, builds
the Windows installer and the macOS disk image, and uploads artifacts. It is advisory. It
does not run on every change under `gleapp/`: a logic change inside an existing module
almost never breaks the frozen build, and the three legs together bill about 35 minutes
per run while the repository is private. It runs when `packaging/`, `pyproject.toml`,
`requirements.txt` or its own file change, every Monday on `main`, and by dispatch.

## Every tracked text file must be LF

`tools/check_line_endings.sh` runs in the required test job. See the cross-platform rules.

## Pull requests from a fork never run CI while the repository is private

GitHub does not run Actions on fork pull requests into a private repository and offers no
switch for it. Push branches to this repository instead; collaborators have write access.

## Reproduce locally

    python -m pytest tests -q
    PYTHONPATH=. python tools/lint_changed.py --base-ref origin/main <changed .py files>
    python tools/ci_import_smoke.py
    bash tools/check_line_endings.sh
