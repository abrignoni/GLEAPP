# Build and release

One driver, `packaging/build.py`, on every platform, in two phases so a signed build is
possible. The PowerShell script it replaced ran only on Windows and rewrote the tracked
spec on every build.

    python packaging/build.py exe               phase 1: dist/GLEAPP/ (one-folder); on macOS also dist/GLEAPP.app
    python packaging/build.py exe --onefile     phase 1: one executable; no installer can be built from this
    python packaging/build.py installer         phase 2: Windows dist/GLEAPP-Setup-<v>.exe, macOS dist/GLEAPP-<v>.dmg
    python packaging/build.py installer --sign-tool NAME
                                                Windows: Inno Setup signs the installer and the uninstaller
    python packaging/build.py all               both phases, unsigned; refuses --sign-tool and --onefile
    python packaging/build.py verify PATH ...   a signature is present and valid; --subject checks the signer

## Signing goes between the phases

Sign `dist/GLEAPP/GLEAPP.exe`, or codesign the `.app`, after phase 1 and before phase 2, or
the installer ships an unsigned executable inside a signed wrapper. `all` refuses
`--sign-tool` for exactly that reason. `verify` is the last step before anything is
uploaded: a signature that is missing or invalid looks like a good one until a user's
machine rejects it.

## What the driver guarantees

The version is read from `gleapp/__init__.py` as text and passed to Inno Setup as
`/DAppVer`; `installer.iss` refuses to compile without it, so the installer and the app
cannot disagree. `ONEFILE` reaches the spec through the `GLEAPP_ONEFILE` environment
variable; the spec is never edited by a build. PyInstaller is pinned in the `[build]`
extra, which phase 1 installs. Every artifact is asserted to exist after the step that
makes it; an exit code is not evidence. `build/` and `dist/` are git-ignored.

## What is and is not wired up

Windows: executable and Inno Setup installer. macOS: `.app` and `.dmg`, unsigned until
codesign. Linux: the one-folder build runs; no AppImage yet, since an AppImage needs an
icon and a `.desktop` file and `packaging/` has no icon. Windows on ARM: the installer
needs an `arm64` architecture variant and a CI leg on an ARM runner; check that runner's
availability and billing for this repository's visibility before adding it. No icon ships
on any platform yet.
