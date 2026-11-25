param(
    [Parameter(Mandatory)][string]$Python,
    [Parameter(Mandatory)][string]$Config,
    [Parameter(Mandatory)][string]$AttemptDb,
    [Parameter(Mandatory)][string]$FenceSeed,
    [Parameter(Mandatory)][string]$OwnerToken,
    [Parameter(Mandatory)][string]$Client,
    [Parameter(Mandatory)][string]$GeminiKey,
    [Parameter(Mandatory)][string]$StateDir,
    [Parameter(Mandatory)][ValidatePattern('^[a-f0-9]{40}$')][string]$ExpectedSha,
    [switch]$Activate
)
$ErrorActionPreference = 'Stop'
if (-not $Activate) { throw 'Explicit approval is required for continuous owner access and task activation.' }
$taskName = 'KakeiboAI-ReceiptReview-Limited'
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) { throw 'An existing task must not be replaced.' }
$repoPath = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$paths = @($Python, $Config, $AttemptDb, $FenceSeed, $OwnerToken, $Client, $GeminiKey)
foreach ($taskPath in $paths) {
    if (-not (Test-Path -LiteralPath $taskPath -PathType Leaf)) { throw 'A required existing file is missing.' }
    if ($taskPath -match '["\r\n]') { throw 'Invalid path.' }
}
if ($StateDir -match '["\r\n]') { throw 'Invalid state path.' }
$taskArguments = @('-B', '-m', 'scripts.run_receipt_review_daily', '--config', $Config,
    '--attempt-db', $AttemptDb, '--fence-seed', $FenceSeed, '--owner-token', $OwnerToken,
    '--client', $Client, '--gemini-key', $GeminiKey, '--state-dir', $StateDir, '--expected-sha', $ExpectedSha)
$argumentText = ($taskArguments | ForEach-Object { '"' + $_ + '"' }) -join ' '
$action = New-ScheduledTaskAction -Execute $Python -Argument $argumentText -WorkingDirectory $repoPath
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 12) `
    -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -Hidden
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description 'General receipt review, one intake per cycle, financial writes OFF.' | Out-Null
Get-ScheduledTask -TaskName $taskName | Select-Object TaskName, State
