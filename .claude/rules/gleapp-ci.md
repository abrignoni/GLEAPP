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

## The wheel is built and compared with the tracked files

Every job installs the package editable, and an editable install reads the checkout, so
a wheel can be missing files while every job stays green. It was: on 2026-09-21 a wheel
built from `pyproject.toml` held 5 of the 798 tracked non-Python files under `gleapp/`.
`web/static/*` does not descend into folders and no pattern covered the licence files,
so it carried the vendored readers and the face models without their licence texts and
none of the 786 files under `web/static/maps`, whose own four licence texts went with
them. setuptools expands each package-data pattern with `glob(..., recursive=True)`, so
`**/*` does descend; measured on setuptools 84.0.0 and on 77.0.1, the oldest release
the declared floor admits (PyPI does not carry 77.0.0).

`tools/check_wheel.py` builds the wheel from `git archive HEAD` in a temporary folder
and fails unless it holds exactly the tracked files under `gleapp/`. It runs in the
`pytest` job before the dependencies are installed, since it needs only pip, and took
5 s locally. It exits 2, not 1, when the wheel could not be built at all, for example
with no package index to fetch setuptools from.

## Pull requests from a fork never run CI while the repository is private

GitHub does not run Actions on fork pull requests into a private repository and offers no
switch for it. Push branches to this repository instead; collaborators have write access.

## Reproduce locally

    python -m pytest tests -q
    PYTHONPATH=. python tools/lint_changed.py --base-ref origin/main <changed .py files>
    python tools/ci_import_smoke.py
    bash tools/check_line_endings.sh
    python tools/check_wheel.py
