# Applique mosquitto.conf du dossier pc_irrigation_bridge vers Program Files
# et redemarre le service Mosquitto. Executer en PowerShell **Administrateur** :
#   clic droit PowerShell -> Executer en tant qu'administrateur
#   cd "...\11master pf\pc_irrigation_bridge"
#   .\apply_mosquitto_config.ps1

$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "mosquitto.conf"
$dstDir = "C:\Program Files\mosquitto"
$dst = Join-Path $dstDir "mosquitto.conf"

if (-not (Test-Path $src)) {
    Write-Error "Fichier introuvable: $src"
}

if (-not (Test-Path $dstDir)) {
    Write-Error "Mosquitto non installe (dossier absent): $dstDir"
}

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    Write-Error "Relancez ce script en mode Administrateur (clic droit sur PowerShell)."
}

if (Test-Path $dst) {
    $bak = "$dst.bak." + (Get-Date -Format "yyyyMMddHHmmss")
    Copy-Item -LiteralPath $dst -Destination $bak -Force
    Write-Host "Sauvegarde: $bak"
}

Copy-Item -LiteralPath $src -Destination $dst -Force
Write-Host "Copie OK -> $dst"

$svc = Get-Service -ErrorAction SilentlyContinue | Where-Object {
    $_.Name -match "mosquitto" -or $_.DisplayName -match "mosquitto"
}
if ($svc) {
    Write-Host "Redemarrage service: $($svc.Name)"
    Restart-Service -Name $svc.Name -Force
    Get-Service -Name $svc.Name | Format-List Name, Status, StartType
} else {
    Write-Warning "Service Mosquitto introuvable. Redemarrez manuellement depuis services.msc (Mosquitto Broker)."
}

Write-Host ""
Write-Host "Verifiez le pare-feu Windows (TCP 1883 entrant) si l'ESP32 ne se connecte toujours pas."
Write-Host "Ne lancez pas un second mosquitto.exe -v si le service tourne (port deja utilise)."
