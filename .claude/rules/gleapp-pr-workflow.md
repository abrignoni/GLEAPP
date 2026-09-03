# Landing a change

Work on a branch cut from `origin/main` and land it through a pull request. `main` is
protected: no direct pushes, for anyone. Branch prefixes follow the repository: `fix/`,
`feat/`, `chore/`, `ci/`, `docs/`. Fetch before branching; `main` moves.

Push branches to this repository, not to a fork. Fork pull requests never run CI while the
repository is private.

Stage explicit paths. Never `git add -A` or a bare directory: a build leaves ignored output
around, and one wrong ignore rule is a careless add away from a 200 MB commit. Read
`git diff --cached --name-only` before committing.

Seven checks are required and zero approvals, so the author merges their own pull request
once they are green. Read `gh pr checks <n>` by name, not by color: a check that never ran
shows nothing red either.

## Attribution

Commits and pull requests produced with an AI coding agent carry a trailer naming the
agent and its version, with the vendor's no-reply address, so the co-author is actually
attributed: `Co-Authored-By: <agent and version> <vendor no-reply address>`. A trailer
with no email renders as plain text. Human co-authors get the usual trailer alongside it.
