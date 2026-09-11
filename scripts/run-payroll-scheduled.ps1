param(
    [string]$ConfigFile = "$env:LOCALAPPDATA\KakeiboAI\Payroll\scheduled-scan-config-v1.json"
)

$ErrorActionPreference = "Stop"
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repositoryRoot ".venv\Scripts\python.exe"

try {
    Set-Location -LiteralPath $repositoryRoot
    if (Test-Path -LiteralPath $venvPython) {
        $pythonExe = $venvPython
    } else {
        $pythonExe = (Get-Command python -ErrorAction Stop).Source
    }
    & $pythonExe -m app.cli payroll-production-scheduled --config-file $ConfigFile
    exit $LASTEXITCODE
} catch {
    Write-Error "Payroll scheduled scan launcher failed."
    exit 2
}
