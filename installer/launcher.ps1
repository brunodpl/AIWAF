<#
.SYNOPSIS
  Wrapper invocado por la tarea programada de autostart y por el instalador.

.PARAMETER Action
  start              — arranca contenedores con docker compose up -d
  stop               — para contenedores con docker compose stop
  status             — docker compose ps
  update             — descarga última imagen GHCR y reinicia
  register-autostart — registra la tarea programada que arranca AIWAF al iniciar Windows

.NOTES
  El primer arranque (build + up + espera de salud) lo hace el instalador
  directamente desde aiwaf-setup.iss con barra de progreso visible. Este
  script ya no expone "first-run" — sólo gestiona ciclo de vida posterior.
#>

param(
  [Parameter(Mandatory = $true)]
  [ValidateSet('start', 'stop', 'status', 'update', 'register-autostart', 'register-update-task')]
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

function Register-UpdateTask {
  $taskName = "AIWAF-Update"
  $action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -File `"$PSScriptRoot\update-task.ps1`""
  # Cada 2 minutos a partir de 1 min después de registrar, indefinidamente
  $trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 2)
  $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopOnIdleEnd `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30)
  Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
    -Settings $settings -RunLevel Highest -Force | Out-Null
  Write-Host "Tarea programada AIWAF-Update registrada (intervalo 2 min)."
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
  'update' {
    Ensure-DockerRunning
    docker compose pull
    if ($LASTEXITCODE -ne 0) { throw "docker compose pull falló: $LASTEXITCODE" }
    docker compose up -d
    exit $LASTEXITCODE
  }
  'register-autostart' {
    Register-Autostart
    exit 0
  }
  'register-update-task' {
    Register-UpdateTask
    exit 0
  }
}
