<#
.SYNOPSIS
  Wrapper invocado por la tarea programada de autostart y por el instalador.

.PARAMETER Action
  start              — arranca contenedores con docker compose up -d
  stop               — para contenedores con docker compose stop
  status             — docker compose ps
  register-autostart — registra la tarea programada que arranca AIWAF al iniciar Windows

.NOTES
  Las actualizaciones las maneja Watchtower (servicio del docker-compose).
  El backend dispara updates on-demand via su HTTP API; este launcher ya no
  participa en el flujo de actualización.
#>

param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('start', 'stop', 'status', 'register-autostart')]
  [string]$Action
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $projectRoot

function Ensure-DockerRunning {
  try {
    $null = docker info 2>$null
    if ($LASTEXITCODE -ne 0) { throw "docker info failed" }
  }
  catch {
    Write-Host "Docker Desktop no está en ejecución. Arrancándolo..." -ForegroundColor Yellow
    $dockerExe = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerExe) {
      Start-Process -FilePath $dockerExe
      $deadline = (Get-Date).AddSeconds(120)
      while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 3
        docker info *> $null
        if ($LASTEXITCODE -eq 0) { return }
      }
      throw "Docker Desktop no respondió en 120s. Arráncalo manualmente y reintenta."
    }
    else {
      throw "Docker Desktop no encontrado. Instálalo desde docker.com y reintenta."
    }
  }
}

function Register-Autostart {
  $taskName = "AIWAF-Autostart"
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\launcher.ps1`" -Action start"
  $trigger = New-ScheduledTaskTrigger -AtLogOn
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest -Force | Out-Null
  Write-Host "Tarea programada AIWAF-Autostart registrada."
}

switch ($Action) {
  'start' {
    Ensure-DockerRunning
    docker compose up -d
    exit $LASTEXITCODE
  }
  'stop' {
    docker compose stop
    exit $LASTEXITCODE
  }
  'status' {
    docker compose ps
    exit $LASTEXITCODE
  }
  'register-autostart' {
    Register-Autostart
    exit 0
  }
}
