param([Parameter(Mandatory = $true)][string]$ConfigFile)
$ErrorActionPreference = 'Stop'
$privateConfig = (Resolve-Path -LiteralPath $ConfigFile).Path
$runFile = $privateConfig + '.run.json'
$run = Get-Content -LiteralPath $runFile -Raw | ConvertFrom-Json
$marker = [System.IO.Path]::GetFullPath($run.stopFile)
$parent = [System.IO.Path]::GetDirectoryName($privateConfig)
if ([System.IO.Path]::GetDirectoryName($marker) -ne $parent -or [System.IO.Path]::GetFileName($marker) -notmatch '^bridge-stop-[0-9a-f]{32}\.signal$') {
    throw 'Unexpected stop marker path; refusing to write it.'
}
Set-Content -LiteralPath $marker -Value 'stop' -Encoding ascii
Write-Output 'Clean stop requested; allow up to 15 seconds for color restoration.'
