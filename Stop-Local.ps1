param([string]$ConfigFile)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'Local-Paths.ps1')
$ConfigFile = Get-LocalGoveeConfig $ConfigFile
& (Join-Path $PSScriptRoot 'ble-companion\Stop-Bridge.ps1') -ConfigFile $ConfigFile
