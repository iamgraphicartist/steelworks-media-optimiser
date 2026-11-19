# Build the Windows installer.  Usage:  powershell -ExecutionPolicy Bypass -File scripts\build_windows.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
python -m pip install -r requirements.txt
python scripts\build.py @args
