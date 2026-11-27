# GLEAPP on Arch Linux (AUR)

Two packages, one for each way of staying current:

| Package | What it installs | Stays current how |
|---|---|---|
| `gleapp-bin` | The official frozen Linux build from the GitHub release | `yay -Syu` picks up each release |
| `gleapp-git` | Built from the tip of `main` at install time | Rebuild with `yay -Syu --devel` to follow every commit |

`-git` is the one that gives you the latest version every time: its
`pkgver()` reads `gleapp/__init__.py` and appends the commit count and hash
(`2026.5.7.r794.g602cdf0`), so any rebuild tracks `main`.

```bash
yay -S gleapp-bin   # the release (most users)
yay -S gleapp-git   # the latest commit (needs several minutes: it builds
                    # the frozen binary from source, like release.yml does)
```

Both install the same layout: `/opt/gleapp/`, `/usr/bin/gleapp`, a
`gleapp.desktop` entry and icons. `gleapp` with no arguments opens the
desktop window where a toolkit is present, `gleapp web` serves the review
gallery to a browser.

## Why frozen builds, not system Python packages

The release binary bundles the exact dependency versions it was built and
smoke-tested with. The important one is OpenCV 5: the content model does not
load on any OpenCV 4.x, which is what most distributions package, so a
system-Python install would silently break similarity search. The frozen
build has no such problem and works offline.

## Publishing these to the AUR

The AUR does not track GitHub: each package lives in its own AUR repository
(`gleapp-bin`, `gleapp-git`). To publish or update:

```bash
git clone ssh://aur@aur.archlinux.org/gleapp-bin.git
cp packaging/aur/gleapp-bin/{PKGBUILD,.SRCINFO} gleapp-bin/
cd gleapp-bin && makepkg --printsrcinfo > .SRCINFO   # must match PKGBUILD
makepkg -s                                            # test the build
namcap PKGBUILD && namcap gleapp-bin-*.pkg.tar.zst   # test the result
git add PKGBUILD .SRCINFO && git commit -m "Upstream release" && git push
```

New release checklist for `gleapp-bin`: bump `pkgver`, replace the first
`sha256sums` entry with the tarball's line from that release's
`SHA256SUMS.txt`, refresh the icon sums only if the logo changed, regenerate
`.SRCINFO`. Nothing else moves: the icon URLs and the inline desktop entry
are pinned to `v$pkgver`.
