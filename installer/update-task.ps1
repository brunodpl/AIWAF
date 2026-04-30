<#
.SYNOPSIS
  Tarea programada que escucha solicitudes de actualización desde la UI.
  Lee data/output/.update_request y, si existe, ejecuta `docker compose pull && up -d`.

.NOTES
  Pensada para registrarse como Scheduled Task que se dispara al arrancar
  Windows + cada 5 minutos. Resuelve la limitación de que el contenedor no
  puede actualizarse a sí mismo.
#>

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

$flagPath = Join-Path $projectRoot "sistema-de-asientos-automatizado\data\output\.update_request"
if (-not (Test-Path $flagPath)) {
  exit 0
}

$logPath = Join-Path $projectRoot "sistema-de-asientos-automatizado\logs\updater.log"
function Log([string]$msg) {
  $stamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:ssK")
  Add-Content -Path $logPath -Value "[$stamp] $msg"
}

try {
  Log "Solicitud de actualización detectada"
  Remove-Item $flagPath -Force
  docker compose pull 2>&1 | ForEach-Object { Log $_ }
  if ($LASTEXITCODE -ne 0) { throw "docker compose pull falló: $LASTEXITCODE" }
  docker compose up -d 2>&1 | ForEach-Object { Log $_ }
  if ($LASTEXITCODE -ne 0) { throw "docker compose up -d falló: $LASTEXITCODE" }
  Log "Actualización completada con éxito"
}
catch {
  Log "ERROR: $_"
  exit 1
}
