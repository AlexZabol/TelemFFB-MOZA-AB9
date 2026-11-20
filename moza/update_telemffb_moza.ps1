<#
  Update / install TelemFFB-MOZA from the latest GitHub release of the fork.

  - Downloads the newest release zip of AlexZabol/VPforce-TelemFFB (tags "moza-*").
  - Installs it into $InstallDir (default: C:\TelemFFB-MOZA), keeping a backup of the previous version.
  - Copies MOZA_SDK.dll (x64) from your own MOZA SDK download into the install folder
    (the DLL is not redistributed in the release).

  Usage:  powershell -ExecutionPolicy Bypass -File update_telemffb_moza.ps1
          [-InstallDir C:\TelemFFB-MOZA] [-SdkZip C:\Users\<you>\Downloads\MOZA_SDK_1.0.0.6_en.zip] [-SdkDll path\MOZA_SDK.dll]
#>
param(
  [string]$Repo = "AlexZabol/VPforce-TelemFFB",
  [string]$InstallDir = "C:\TelemFFB-MOZA",
  [string]$SdkZip = "",
  [string]$SdkDll = ""
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$headers = @{ "User-Agent" = "telemffb-moza-updater" }

Write-Host "Checking latest release of $Repo ..."
$rels = Invoke-RestMethod "https://api.github.com/repos/$Repo/releases?per_page=20" -Headers $headers
$rel = $rels | Where-Object { $_.tag_name -like "moza-*" -and -not $_.draft } | Select-Object -First 1
if (-not $rel) { throw "No moza-* release found in $Repo" }
$asset = $rel.assets | Where-Object { $_.name -like "*.zip" } | Select-Object -First 1
$tag = $rel.tag_name

$stamp = Join-Path $InstallDir "installed_version.txt"
if ((Test-Path $stamp) -and ((Get-Content $stamp -Raw).Trim() -eq $tag)) {
  Write-Host "Already up to date: $tag"
} else {
  if (Get-Process -Name "VPforce-TelemFFB" -ErrorAction SilentlyContinue | Where-Object { $_.Path -like "$InstallDir*" }) {
    throw "TelemFFB-MOZA is running from $InstallDir - close it and run the update again."
  }
  $tmp = Join-Path $env:TEMP "telemffb-moza-update"
  if (Test-Path $tmp) { Remove-Item $tmp -Recurse -Force }
  New-Item -ItemType Directory -Force $tmp | Out-Null
  $zip = Join-Path $tmp $asset.name
  Write-Host "Downloading $($asset.name) ..."
  Invoke-WebRequest $asset.browser_download_url -OutFile $zip -Headers $headers
  Expand-Archive $zip (Join-Path $tmp "new") -Force
  if (Test-Path $InstallDir) {
    $bak = "$InstallDir.previous"
    if (Test-Path $bak) { Remove-Item $bak -Recurse -Force }
    Move-Item $InstallDir $bak
    Write-Host "Previous version kept in $bak"
  }
  Move-Item (Join-Path $tmp "new") $InstallDir
  Set-Content $stamp $tag -Encoding ascii
  Write-Host "Installed $tag into $InstallDir"
}

# --- MOZA_SDK.dll from the user's own SDK download -----------------------------------
$target = Join-Path $InstallDir "MOZA_SDK.dll"
if ($SdkDll -and (Test-Path $SdkDll)) {
  Copy-Item $SdkDll $target -Force
} elseif (-not (Test-Path $target)) {
  if (-not $SdkZip) {
    $SdkZip = Get-ChildItem "$env:USERPROFILE\Downloads" -Filter "MOZA_SDK_*.zip" -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1 -ExpandProperty FullName
  }
  $prev = "$InstallDir.previous\MOZA_SDK.dll"
  if (Test-Path $prev) {
    Copy-Item $prev $target -Force
  } elseif ($SdkZip -and (Test-Path $SdkZip)) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $z = [IO.Compression.ZipFile]::OpenRead($SdkZip)
    try {
      $e = $z.Entries | Where-Object { $_.FullName -like "*x64-release/bin/MOZA_SDK.dll" } | Select-Object -First 1
      if (-not $e) { throw "x64-release/bin/MOZA_SDK.dll not found in $SdkZip" }
      [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
    } finally { $z.Dispose() }
  } else {
    Write-Warning "MOZA_SDK.dll not found. Download the MOZA SDK from https://mozaracing.com/pages/sdk and rerun with -SdkZip <path>."
  }
}
if (Test-Path $target) { Write-Host "MOZA_SDK.dll present." }
Write-Host "Done. Start: $InstallDir\VPforce-TelemFFB.exe"
