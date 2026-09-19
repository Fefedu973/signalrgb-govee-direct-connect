function Get-LocalGoveeConfig([string]$ExplicitPath) {
    if ($ExplicitPath) { return [IO.Path]::GetFullPath($ExplicitPath) }
    $shared = Join-Path (Split-Path $PSScriptRoot -Parent) 'SignalRGB-Local-Bridges\local-installation\govee\config.local.json'
    if (Test-Path -LiteralPath $shared -PathType Leaf) { return $shared }
    return (Join-Path $env:LOCALAPPDATA 'GoveeBleBridge\config.local.json')
}
