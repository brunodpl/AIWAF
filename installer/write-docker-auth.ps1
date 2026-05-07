<#
.SYNOPSIS
  Escribe un docker config.json mínimo con auth base64 explícita en $ConfigDir.
  Watchtower (contenedor Linux) lo monta como /config.json para autenticarse en GHCR.

.NOTES
  No reusamos ~/.docker/config.json porque Docker Desktop en Windows almacena el token
  en el credential manager del SO, no en el archivo. Watchtower no puede leer ese
  credential manager desde un contenedor Linux.
#>
param(
  [Parameter(Mandatory = $true)] [string]$Token,
  [Parameter(Mandatory = $true)] [string]$ConfigDir,
  [string]$Username = 'brunodpl'
)

$ErrorActionPreference = 'Stop'
$auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("$Username`:$Token"))
$json = '{"auths":{"ghcr.io":{"auth":"' + $auth + '"}}}'
New-Item -ItemType Directory -Path $ConfigDir -Force | Out-Null

# Defensivo: si docker compose se levantó antes que este script, Watchtower
# montó ./.docker/config.json como bind y Docker creó un DIRECTORIO vacío en
# esa ruta cuando el archivo no existía aún. Detectarlo y borrarlo antes de
# intentar escribir el archivo, o el Set-Content fallará silenciosamente.
$ConfigPath = Join-Path $ConfigDir 'config.json'
if ((Test-Path $ConfigPath) -and ((Get-Item $ConfigPath).PSIsContainer)) {
  Write-Warning "config.json existe como directorio (probablemente Watchtower lo creó al arrancar antes que este script). Eliminándolo."
  Remove-Item -Recurse -Force $ConfigPath
}

Set-Content -Path $ConfigPath -Value $json -Encoding ascii -NoNewline
Write-Host "Docker auth para Watchtower escrito en $ConfigPath"
