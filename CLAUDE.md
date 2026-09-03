# GLEAPP

Media triage for digital forensics: cryptographic and perceptual hashes, exact and
near-duplicate grouping, EXIF and GPS, face and skin screening, a local review gallery,
Project VIC and NSRL hash lists, and HTML, CSV, JSON and KML reports. A Python package
(`gleapp/`) with a Flask web UI and a pywebview desktop shell, frozen with PyInstaller.

## Cross-platform first

GLEAPP ships on Windows, macOS and Linux, and Windows on ARM is planned. A change is not
done when it works on one of them. The suite runs on Linux and Windows in CI, the frozen
build is exercised on all three, and the traps that have actually bitten are written down
in `.claude/rules/gleapp-cross-platform.md`, each with the measurement behind it. Read that
file before touching paths, line endings, packaging, or anything that imports a library
that exists on only one platform.

## Read these before changing things

| file | covers |
| --- | --- |
| `.claude/rules/gleapp-cross-platform.md` | the platform traps, measured, and the guard for each |
| `.claude/rules/gleapp-ci.md` | what CI enforces and why it is shaped the way it is |
| `.claude/rules/gleapp-build-and-release.md` | the two-phase build, the signing gap, what each platform produces |
| `.claude/rules/gleapp-pr-workflow.md` | branches, pull requests, staging, attribution |

If anything here contradicts the code or a workflow file, the code wins and this is stale.

## Running from source

The same steps as the other LEAPPs: clone, venv, `pip install -r requirements.txt`, then
`python gleapp.py web` for the browser gallery or `python gleappGUI.py` for the native
window, which needs `pip install -e .[desktop]`. The README's Install section says which
setups need a C compiler and why.

## Tests

`python -m pytest tests -q`. The pipeline tests build a synthetic evidence set with
`tools/make_sample_evidence.py`. Nothing in this repository is real evidence and nothing
should ever be committed that is, including values quoted in notes or pull requests.
