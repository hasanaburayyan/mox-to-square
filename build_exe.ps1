# Builds dist\MtgSync\MtgSync.exe and copies config + .env next to it.
# Run from the project folder:  .\build_exe.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# PyInstaller logs to stderr; don't let PowerShell 5.1 treat that as a failure.
$ErrorActionPreference = "Continue"
& "$root\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed `
    --name MtgSync --paths "$root" run_app.py 2>&1 | ForEach-Object { "$_" } | Select-Object -Last 3
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE" }
$ErrorActionPreference = "Stop"

$dist = "$root\dist\MtgSync"
New-Item -ItemType Directory -Force "$dist\config" | Out-Null
Copy-Item "$root\config\settings.yaml" "$dist\config\settings.yaml" -Force
Copy-Item "$root\config\set_code_overrides.yaml" "$dist\config\set_code_overrides.yaml" -Force
Copy-Item "$root\.env.example" "$dist\.env.example" -Force
if (Test-Path "$root\.env") { Copy-Item "$root\.env" "$dist\.env" -Force }
foreach ($d in "cache", "work", "output", "state") { New-Item -ItemType Directory -Force "$dist\$d" | Out-Null }
# Carry the batch guards and image/tracked state across, unless the dist already has newer ones.
foreach ($f in Get-ChildItem "$root\state\*.json" -ErrorAction SilentlyContinue) {
    if (-not (Test-Path "$dist\state\$($f.Name)")) { Copy-Item $f.FullName "$dist\state\" }
}
# Reuse the Scryfall cache so the first run does not download again.
if (Test-Path "$root\cache\scryfall_index.json") {
    Copy-Item "$root\cache\scryfall_default_cards.jsonl.gz" "$dist\cache\" -Force -ErrorAction SilentlyContinue
    Copy-Item "$root\cache\scryfall_index.json" "$dist\cache\" -Force
}
Write-Host "Built $dist\MtgSync.exe"
