$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $Python)) {
    throw 'Client dependencies are missing. Run .\setup.ps1 first.'
}

& $Python -m pip install 'pyinstaller>=6.0,<7.0'
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller dependency installation failed with exit code $LASTEXITCODE."
}
& $Python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'SMSIArchiveClient' `
    --collect-all keyring `
    --collect-all pyarrow `
    --collect-all tzdata `
    (Join-Path $ProjectRoot 'main.py')
if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller build failed with exit code $LASTEXITCODE. Close the running client before rebuilding."
}

Copy-Item `
    -Path (Join-Path $ProjectRoot '*.md') `
    -Destination (Join-Path $ProjectRoot 'dist') `
    -Force

Write-Host ('Build complete: ' + (Join-Path $ProjectRoot 'dist\SMSIArchiveClient.exe'))
