@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ble-companion\Stop-Bridge.ps1" -ConfigFile "%LOCALAPPDATA%\GoveeBleBridge\config.local.json"
if errorlevel 1 pause
