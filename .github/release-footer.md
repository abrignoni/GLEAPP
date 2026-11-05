
---

## Which file to download

| Platform | File |
|---|---|
| Windows 10 or 11, 64-bit | `-windows-x64-portable.zip` |
| macOS, Apple silicon | `-macos-arm64.dmg` |
| macOS, Intel | `-macos-x64.dmg` |
| Linux, 64-bit | `-linux-x64.tar.gz` |
| Debian, Ubuntu and derivatives, 64-bit | `-amd64.deb` |
| Fedora, RHEL, openSUSE, 64-bit | `-x86_64.rpm` |
| Arch Linux | AUR `gleapp-bin` (release) or `gleapp-git` (every commit) |

The `.deb`, the `.rpm` and the `.tar.gz` are the same frozen Linux build,
installed to `/opt/gleapp` with a `gleapp` command on `PATH`.

**On Linux, one line installs or updates to the latest release**, picking the
right file for your distribution and checking it against `SHA256SUMS.txt`:

```bash
curl -fsSL https://raw.githubusercontent.com/abrignoni/GLEAPP/main/packaging/install.sh | bash
```

Pipe to `bash -s -- --user` for `~/.local` with no root, `--uninstall` to
remove it, or pin a version with `GLEAPP_VERSION=...`. On Arch you can use an
AUR helper instead: `yay -S gleapp-bin` for the release, `yay -S gleapp-git`
for the latest commit (`yay -Syu --devel` rebuilds it, so you always have the
newest version).

**On Windows the zip holds the whole program as one file, `GLEAPP.exe`**, which can be put
wherever you like; there is no installer any more. If an earlier release's installer put
GLEAPP in Program Files, uninstall it from Settings, Apps; your cases and settings are kept
elsewhere and stay. `GLEAPP.exe` unpacks itself to a temporary folder each time it starts,
so it is slower to open than an installed copy was, and it does not run where a policy
(AppLocker, WDAC) forbids running programs from the temporary folder. There, run GLEAPP
from source; the README has the steps.

## First launch

The macOS disk images are signed with a Developer ID and notarised by Apple, so they
open without a warning.

The Windows binaries are not signed yet, so SmartScreen says "Windows protected your
PC" the first time. Choose More info, then Run anyway. If you would rather not clear a
warning, run from source instead; the README has the steps.

## Verify what you downloaded

`SHA256SUMS.txt` covers every binary in this release. On macOS or Linux, from the folder
you downloaded into:

```bash
grep <the file you downloaded> SHA256SUMS.txt | shasum -a 256 -c -
```

Use `sha256sum` in place of `shasum -a 256` on Linux. On Windows, in PowerShell:

```powershell
(Get-FileHash -Algorithm SHA256 .\<the file you downloaded>).Hash
```

and compare it with the line in `SHA256SUMS.txt`, which is lower case.

## Linux

The build is made on Ubuntu 24.04, so it needs glibc 2.39 or newer and will not start on
an older distribution; the `.deb`, the `.rpm` and the `.tar.gz` all carry that
same build. The native desktop window needs a GTK or Qt webview toolkit that
pip does not install, and CI does not exercise it, so `gleapp web` in a browser is the
tested path there.

## Code signing policy

The [code signing policy](https://github.com/abrignoni/GLEAPP#code-signing-policy) covers
Windows signing, team roles and privacy.
