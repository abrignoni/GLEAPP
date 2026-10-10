
---

## Which file to download

| Platform | File |
|---|---|
| Windows 10 or 11, 64-bit | `-windows-x64-setup.exe` (installer), or `-windows-x64-portable.zip` to run without installing |
| macOS, Apple silicon | `-macos-arm64.dmg` |
| macOS, Intel | `-macos-x64.dmg` |
| Linux, 64-bit | `-linux-x64.tar.gz` |

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
an older distribution. The native desktop window needs a GTK or Qt webview toolkit that
pip does not install, and CI does not exercise it, so `gleapp web` in a browser is the
tested path there.

## Code signing policy

The [code signing policy](https://github.com/abrignoni/GLEAPP#code-signing-policy) covers
Windows signing, team roles and privacy.
