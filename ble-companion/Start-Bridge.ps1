param(
    [Parameter(Mandatory = $true)][string]$ConfigFile,
    [string]$Python = 'python',
    [switch]$Background,
    [ValidateRange(0,86400)][int]$Seconds = 0
)
$ErrorActionPreference = 'Stop'
$privateConfig = (Resolve-Path -LiteralPath $ConfigFile).Path
$entryPoint = Join-Path $PSScriptRoot 'bridge.py'
$runFile = $privateConfig + '.run.json'
$stopFile = Join-Path (Split-Path $privateConfig) ('bridge-stop-' + [guid]::NewGuid().ToString('N') + '.signal')
$stdoutFile = $privateConfig + '.stdout.log'
$stderrFile = $privateConfig + '.stderr.log'
if (Test-Path -LiteralPath $runFile) {
    $previousRun = Get-Content -LiteralPath $runFile -Raw | ConvertFrom-Json
    $previousProcess = Get-Process -Id $previousRun.pid -ErrorAction SilentlyContinue
    if ($previousProcess -and $previousProcess.StartTime.ToUniversalTime().Ticks -eq ([datetime]$previousRun.processStartedUtc).ToUniversalTime().Ticks) {
        throw 'This configuration already has a running bridge. Use Stop-Bridge.ps1 first.'
    }
}
& $Python $entryPoint --config $privateConfig
if ($LASTEXITCODE -ne 0) { throw 'Configuration validation failed.' }
$arguments = @(('"' + $entryPoint + '"'), '--config', ('"' + $privateConfig + '"'), '--serve', '--stop-file', ('"' + $stopFile + '"'))
if ($Seconds -gt 0) { $arguments += @('--seconds', $Seconds.ToString()) }
if ($Background) {
    $bridgeProcess = Start-Process -FilePath $Python -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutFile -RedirectStandardError $stderrFile
    $info = [ordered]@{pid=$bridgeProcess.Id;processStartedUtc=$bridgeProcess.StartTime.ToUniversalTime().ToString('o');stopFile=$stopFile;stdout=$stdoutFile;stderr=$stderrFile}
    $info | ConvertTo-Json | Set-Content -LiteralPath $runFile -Encoding utf8
    Write-Output ('Bridge started in background. PID: ' + $bridgeProcess.Id)
    Write-Output ('Stop cleanly: .\Stop-Bridge.ps1 -ConfigFile "' + $privateConfig + '"')
} else {
    Write-Output ('Clean stop marker: ' + $stopFile)
    if ($Seconds -gt 0) {
        & $Python $entryPoint --config $privateConfig --serve --stop-file $stopFile --seconds $Seconds
    } else {
        & $Python $entryPoint --config $privateConfig --serve --stop-file $stopFile
    }
    exit $LASTEXITCODE
}
