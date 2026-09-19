param([string]$ConfigFile=(Join-Path $env:LOCALAPPDATA 'GoveeBleBridge\config.local.json'))
$ErrorActionPreference='Stop'
$python=Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if(!(Test-Path -LiteralPath $python)){throw 'Creer .venv et installer ble-companion/requirements-windows-py313.lock.txt avant le premier lancement.'}
if(!(Test-Path -LiteralPath $ConfigFile)){throw 'Configuration privee introuvable. Voir ble-companion/README.md.'}
if(Get-NetUDPEndpoint -LocalAddress '127.0.0.1' -LocalPort 47684 -ErrorAction SilentlyContinue){Write-Output 'Un pont utilise deja UDP47684.';exit 0}
& (Join-Path $PSScriptRoot 'ble-companion\Start-Bridge.ps1') -ConfigFile $ConfigFile -Python $python -Background
