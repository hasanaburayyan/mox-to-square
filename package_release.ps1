# Zips dist\MtgSync for sharing, WITHOUT the Square token (.env), caches or local state.
# Run .\build_exe.ps1 first. Output: dist\MtgSync-<version>.zip
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$dist = "$root\dist\MtgSync"
if (-not (Test-Path "$dist\MtgSync.exe")) { throw "Build first: .\build_exe.ps1" }
$version = (Select-String -Path "$root\mtgsync\__init__.py" -Pattern '__version__ = "([^"]+)"').Matches[0].Groups[1].Value
$stage = "$root\dist\MtgSync-release"
if (Test-Path $stage) { Remove-Item $stage -Recurse -Force }
New-Item -ItemType Directory $stage | Out-Null
# Everything except secrets and machine-local data.
Copy-Item "$dist\MtgSync.exe" $stage -Force
Copy-Item "$dist\_internal" "$stage\_internal" -Recurse -Force
New-Item -ItemType Directory -Force "$stage\config" | Out-Null
Copy-Item "$root\config\settings.yaml" "$stage\config\settings.yaml" -Force
Copy-Item "$root\config\set_code_overrides.yaml" "$stage\config\set_code_overrides.yaml" -Force
Copy-Item "$root\.env.example" "$stage\.env.example" -Force
if (-not (Test-Path "$stage\config\settings.yaml")) { throw "config\settings.yaml missing from the release" }
@"
MTG Sync $version  (Moxfield -> Square)

1. Unzip anywhere, e.g. C:\MtgSync.
2. Run MtgSync.exe. Windows SmartScreen may say "Windows protected your PC" because the
   app is not code-signed: click "More info", then "Run anyway".
3. Click "Square settings...", paste the Square access token for the store's account,
   click "Test connection" (it should list the store's location), then Save.
4. Export the collection from Moxfield, choose the CSV, click "Upload to Square".
   The first run downloads about 80 MB of card data from Scryfall.
5. Clear the Moxfield collection after every successful upload.

Prices are not sent; set them in Square. Each upload only adds cards; nothing is removed.
"@ | Set-Content "$stage\README.txt" -Encoding UTF8
$zip = "$root\dist\MtgSync-$version.zip"
if (Test-Path $zip) { Remove-Item $zip -Force }
Compress-Archive -Path "$stage\*" -DestinationPath $zip
Remove-Item $stage -Recurse -Force
Write-Host "Wrote $zip ($([math]::Round((Get-Item $zip).Length / 1MB)) MB)"
