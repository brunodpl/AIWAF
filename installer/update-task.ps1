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

  # En PS 5.1, $ErrorActionPreference='Stop' convierte cada línea de stderr de un
  # exe nativo en ErrorRecord terminante. Docker compose escribe progreso a stderr.
  # Cambiamos a 'Continue' alrededor de las llamadas para capturar el exit code real.
  $prevEAP = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  try {
    Log "docker compose pull --ignore-pull-failures"
    & docker compose pull --ignore-pull-failures 2>&1 | ForEach-Object { Add-Content -Path $logPath -Value $_.ToString() }
    $pullExit = $LASTEXITCODE
    if ($pullExit -ne 0) {
      Log "WARN: pull terminó con código $pullExit — continuando con up -d"
    }
    Log "docker compose up -d"
    & docker compose up -d 2>&1 | ForEach-Object { Add-Content -Path $logPath -Value $_.ToString() }
    $upExit = $LASTEXITCODE
  }
  finally {
    $ErrorActionPreference = $prevEAP
  }
  if ($upExit -ne 0) { throw "docker compose up -d falló: $upExit" }
  Log "Actualización completada con éxito"
}
catch {
  Log "ERROR: $_"
  exit 1
}
