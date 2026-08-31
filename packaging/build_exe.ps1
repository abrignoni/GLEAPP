<#
  Build GLEAPP.exe with PyInstaller.

  Usage (from repo root or anywhere):
      .\packaging\build_exe.ps1                # one-folder build
      .\packaging\build_exe.ps1 -OneFile       # single .exe (slower start)
      .\packaging\build_exe.ps1 -Installer     # also run Inno Setup

  Requires: the project venv with dev + desktop deps installed:
      .\.venv\Scripts\python -m pip install -r requirements.txt pyinstaller pywebview
#>
param(
    [switch]$OneFile,
    [switch]$Installer,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo

$py = Join-Path $repo ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { $py = "python" }

if ($Clean) {
    Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
}

Write-Host "==> Checking build deps" -ForegroundColor Cyan
& $py -m pip install -q --disable-pip-version-check pyinstaller pywebview

# Toggle ONEFILE in the spec if requested
$spec = Join-Path $PSScriptRoot "gleapp.spec"
if ($OneFile) {
    (Get-Content $spec) -replace '^ONEFILE = .*', 'ONEFILE = True' | Set-Content $spec
} else {
    (Get-Content $spec) -replace '^ONEFILE = .*', 'ONEFILE = False' | Set-Content $spec
}

Write-Host "==> Running PyInstaller" -ForegroundColor Cyan
& $py -m PyInstaller $spec --noconfirm --distpath (Join-Path $repo "dist") `
      --workpath (Join-Path $repo "build")

$exe = if ($OneFile) { "dist\GLEAPP.exe" } else { "dist\GLEAPP\GLEAPP.exe" }
if (Test-Path $exe) {
    Write-Host "==> Built $exe" -ForegroundColor Green
    Get-Item $exe | Select-Object FullName, Length
} else {
    throw "Build produced no exe at $exe"
}

if ($Installer) {
    $iscc = (Get-Command iscc.exe -ErrorAction SilentlyContinue).Source
    if (-not $iscc) { $iscc = "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" }
    if (Test-Path $iscc) {
        Write-Host "==> Building installer with Inno Setup" -ForegroundColor Cyan
        & $iscc (Join-Path $PSScriptRoot "installer.iss")
    } else {
        Write-Warning "Inno Setup (iscc.exe) not found - skipping installer. Get it from https://jrsoftware.org/isdl.php"
    }
}
