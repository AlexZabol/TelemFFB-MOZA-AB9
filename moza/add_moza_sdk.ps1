<#
  Copy MOZA_SDK.dll (x64) from your MOZA SDK download next to VPforce-TelemFFB.exe.
  Usage: right-click > Run with PowerShell, or
         powershell -ExecutionPolicy Bypass -File add_moza_sdk.ps1 [-SdkZip path\MOZA_SDK_x.x.x.x_en.zip]
#>
param([string]$SdkZip = "")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$target = Join-Path $here "MOZA_SDK.dll"
try {
  if (-not $SdkZip) {
    $SdkZip = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "MOZA_SDK_*.zip" -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
  }
  if (-not $SdkZip -or -not (Test-Path $SdkZip)) {
    throw "MOZA SDK zip not found. Download it from https://mozaracing.com/pages/sdk and run again with -SdkZip <path>."
  }
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $z = [IO.Compression.ZipFile]::OpenRead($SdkZip)
  try {
    $e = $z.Entries | Where-Object { $_.FullName -like "*x64-release/bin/MOZA_SDK.dll" } | Select-Object -First 1
    if (-not $e) { throw "x64-release/bin/MOZA_SDK.dll not found in $SdkZip" }
    [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
  } finally { $z.Dispose() }
  Write-Host "MOZA_SDK.dll copied to $here" -ForegroundColor Green
} catch {
  Write-Host $_ -ForegroundColor Red
}
Read-Host "Press Enter to close"
