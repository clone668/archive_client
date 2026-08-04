$ErrorActionPreference = 'Stop'
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$VenvPython = Join-Path $ProjectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $VenvPython)) {
    python -m venv (Join-Path $ProjectRoot '.venv')
}

& $VenvPython -m pip install --upgrade pip
& $VenvPython -m pip install -r (Join-Path $ProjectRoot 'requirements.txt')

if (-not (Get-Command rclone -ErrorAction SilentlyContinue)) {
    $Winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($Winget) {
        winget install --id Rclone.Rclone --exact --accept-source-agreements --accept-package-agreements
        $MachinePath = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $UserPath = [Environment]::GetEnvironmentVariable('Path', 'User')
        $env:Path = $MachinePath + ';' + $UserPath
    } else {
        Write-Warning 'winget was not found. Install rclone from https://rclone.org/downloads/.'
    }
}

Write-Host 'Setup complete. Run .\start.ps1 to launch the client.'
