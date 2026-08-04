$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Pythonw = Join-Path $ProjectRoot '.venv\Scripts\pythonw.exe'
$Python = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$Entrypoint = Join-Path $ProjectRoot 'main.py'

if (Test-Path -LiteralPath $Pythonw) {
    Start-Process -FilePath $Pythonw -ArgumentList @($Entrypoint) -WorkingDirectory $ProjectRoot
} elseif (Test-Path -LiteralPath $Python) {
    Start-Process -FilePath $Python -ArgumentList @($Entrypoint) -WorkingDirectory $ProjectRoot
} else {
    throw 'Client dependencies are missing. Run .\setup.ps1 first.'
}
