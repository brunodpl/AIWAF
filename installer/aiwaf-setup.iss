; ============================================================================
; AIWAF Installer - Inno Setup script
; Genera aiwaf-setup.exe con asistente gráfico para gestorías no técnicas.
; Compilar con: iscc.exe installer/aiwaf-setup.iss (o usar build.ps1)
; ============================================================================

#define MyAppName       "AIWAF"
#define MyAppVersion    "0.1.0"
#define MyAppPublisher  "estimula"
#define MyAppURL        "https://estimula.es"
#define MyAppExeName    "AIWAF Iniciar"
#define InstallDir      "C:\AIWAF"

[Setup]
AppId={{8A4F2D7B-3C9E-4A1B-B5D2-7E8F9A0B1C2D}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
DefaultDirName={#InstallDir}
DisableDirPage=yes
DisableProgramGroupPage=yes
DefaultGroupName={#MyAppName}
OutputDir=..\dist
OutputBaseFilename=aiwaf-setup-{#MyAppVersion}
Compression=lzma2/ultra
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
SetupLogging=yes

[Languages]
Name: "spanish"; MessagesFile: "compiler:Languages\Spanish.isl"

[Files]
; Proyecto completo (excluyendo carpetas inútiles para el cliente)
Source: "..\sistema-de-asientos-automatizado\*"; DestDir: "{app}\sistema-de-asientos-automatizado"; \
  Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "credentials\*,horeca_sandbox\*\*,data\output\*,logs\*,__pycache__,*.pyc,.pytest_cache,.git*,tests\*"
Source: "..\interfaz-asientos-automatizados\*"; DestDir: "{app}\interfaz-asientos-automatizados"; \
  Flags: ignoreversion recursesubdirs createallsubdirs; \
  Excludes: "node_modules\*,.next\*,.git*,*.log"
Source: "..\docker-compose.yml"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\Makefile"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\verify.ps1"; DestDir: "{app}"; Flags: ignoreversion
Source: "launcher.ps1";   DestDir: "{app}\installer"; Flags: ignoreversion
; Iconos y assets visuales (favicon usado por los accesos directos)
Source: "assets\*"; DestDir: "{app}\installer\assets"; Flags: ignoreversion skipifsourcedoesntexist

[Dirs]
Name: "{app}\sistema-de-asientos-automatizado\credentials"; Permissions: users-modify
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\PENDIENTES\gastos"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\PENDIENTES\ingresos"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\PENDIENTES\bienes"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\20_COMPRAS_GASTOS"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\21_VENTAS_INGRESOS"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\22_BIENES_INVERSION"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\90_PROCESADAS"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\99_INCIDENCIAS"
Name: "{app}\sistema-de-asientos-automatizado\horeca_sandbox\00_PENDIENTE_CLASIFICAR"
Name: "{app}\sistema-de-asientos-automatizado\data\output"; Permissions: users-modify
Name: "{app}\sistema-de-asientos-automatizado\data\feedback"; Permissions: users-modify
Name: "{app}\sistema-de-asientos-automatizado\logs\audit"; Permissions: users-modify

[Icons]
; Único acceso directo: el sistema arranca solo (autostart Windows + ya queda
; corriendo tras la instalación). El usuario solo necesita "Abrir interfaz".
Name: "{commondesktop}\AIWAF Abrir interfaz"; \
  Filename: "{sys}\rundll32.exe"; Parameters: "url.dll,FileProtocolHandler http://localhost:3003"; \
  IconFilename: "{app}\installer\assets\aiwaf.ico"; IconIndex: 0; Comment: "Abrir AIWAF en el navegador"
Name: "{group}\AIWAF Abrir interfaz"; \
  Filename: "{sys}\rundll32.exe"; Parameters: "url.dll,FileProtocolHandler http://localhost:3003"; \
  IconFilename: "{app}\installer\assets\aiwaf.ico"; IconIndex: 0

[Run]
; Tras la instalación, abre la interfaz en el navegador. El arranque de los
; contenedores ya se ha hecho durante el progreso (ssPostInstall, ver [Code]).
Filename: "{sys}\rundll32.exe"; Parameters: "url.dll,FileProtocolHandler http://localhost:3003"; \
  Description: "Abrir AIWAF en el navegador"; \
  Flags: postinstall skipifsilent nowait

[UninstallRun]
; Al desinstalar, parar contenedores
Filename: "powershell.exe"; \
  Parameters: "-ExecutionPolicy Bypass -WindowStyle Hidden -File ""{app}\installer\launcher.ps1"" -Action stop"; \
  WorkingDir: "{app}"; \
  Flags: runhidden; \
  RunOnceId: "stop-aiwaf"

; ============================================================================
; Lógica de configuración: pide credenciales antes de copiar archivos
; ============================================================================
[Code]
var
  CredentialsPage: TInputFileWizardPage;
  ProjectIdPage: TInputQueryWizardPage;
  CredentialsFilePath: string;
  ProjectId: string;
  GestoriaNif: string;
  GestoriaNombre: string;
  WebhookUrl: string;
  PreInstallPage: TOutputMsgWizardPage;

function GenerateRandomHex(Length: Integer): string;
var
  i: Integer;
  Charset: string;
begin
  Charset := '0123456789abcdef';
  Result := '';
  for i := 1 to Length do
    Result := Result + Charset[Random(16) + 1];
end;

function IsDockerInstalled(): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec('cmd.exe', '/c docker --version', '', SW_HIDE,
                 ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

procedure InitializeWizard();
begin
  // Página: pre-requisito Docker
  PreInstallPage := CreateOutputMsgPage(wpWelcome,
    'Antes de continuar',
    'Comprobación de requisitos previos',
    'Esta aplicación necesita Docker Desktop instalado y en ejecución.' + #13#10 + #13#10 +
    'Si Docker Desktop no está instalado, cancela ahora y descárgalo desde:' + #13#10 +
    'https://www.docker.com/products/docker-desktop/' + #13#10 + #13#10 +
    'Después de instalar Docker, arráncalo y espera a que el icono de la ballena' + #13#10 +
    'esté verde en la barra de tareas. Luego vuelve a ejecutar este instalador.');

  // Página: archivo de credenciales
  CredentialsPage := CreateInputFilePage(PreInstallPage.ID,
    'Archivo de credenciales AIWAF',
    'Selecciona el archivo .json que te ha enviado AIWAF',
    'AIWAF te ha enviado un archivo llamado service_account.json (o similar).' + #13#10 +
    'Búscalo en tu correo o en el USB que te entregamos y selecciónalo aquí.' + #13#10 + #13#10 +
    'Sin este archivo, el sistema no podrá procesar facturas.');
  CredentialsPage.Add('Archivo de credenciales:', 'Archivos JSON|*.json|Todos los archivos|*.*', '.json');

  // Página: identificación de la gestoría (para feedback + soporte)
  ProjectIdPage := CreateInputQueryPage(CredentialsPage.ID,
    'Identificación de la gestoría',
    'Datos para soporte y notificaciones',
    'Esta información solo se usa para identificarte en los mensajes de soporte ' +
    'que envíes desde la aplicación. No se comparte con terceros.');
  ProjectIdPage.Add('Nombre de la gestoría:', False);
  ProjectIdPage.Add('NIF de la gestoría:', False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  FileContents: AnsiString;
begin
  Result := True;

  if CurPageID = PreInstallPage.ID then begin
    if not IsDockerInstalled() then begin
      MsgBox('Docker no está instalado o no responde. Instala Docker Desktop, ' +
             'arráncalo y vuelve a ejecutar este instalador.',
             mbError, MB_OK);
      Result := False;
    end;
  end
  else if CurPageID = CredentialsPage.ID then begin
    CredentialsFilePath := CredentialsPage.Values[0];
    if (CredentialsFilePath = '') or (not FileExists(CredentialsFilePath)) then begin
      MsgBox('Selecciona el archivo service_account.json antes de continuar.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    // Validación básica: debe ser JSON con campos esperados
    if not LoadStringFromFile(CredentialsFilePath, FileContents) then begin
      MsgBox('No se pudo leer el archivo. Selecciona otro.', mbError, MB_OK);
      Result := False;
      Exit;
    end;
    if (Pos('"private_key"', FileContents) = 0) or
       (Pos('"client_email"', FileContents) = 0) or
       (Pos('"project_id"', FileContents) = 0) then begin
      MsgBox('El archivo no parece un service_account.json válido. ' +
             'Asegúrate de seleccionar el archivo correcto enviado por AIWAF.',
             mbError, MB_OK);
      Result := False;
      Exit;
    end;
  end
  else if CurPageID = ProjectIdPage.ID then begin
    GestoriaNombre := Trim(ProjectIdPage.Values[0]);
    GestoriaNif := Trim(ProjectIdPage.Values[1]);
    if GestoriaNombre = '' then begin
      MsgBox('Indica el nombre de la gestoría para poder identificar tus mensajes.', mbError, MB_OK);
      Result := False;
    end;
  end;
end;

function ExtractProjectIdFromCredentials(const Path: string): string;
var
  Contents: AnsiString;
  Idx, EndIdx, Start: Integer;
  Pattern: string;
begin
  Result := '';
  if not LoadStringFromFile(Path, Contents) then Exit;
  Pattern := '"project_id"';
  Idx := Pos(Pattern, Contents);
  if Idx = 0 then Exit;
  // Avanzamos hasta el primer ':' tras la clave, luego primera comilla
  Start := Idx + Length(Pattern);
  Idx := Pos('"', Copy(Contents, Start, MaxInt));
  if Idx = 0 then Exit;
  Start := Start + Idx; // posición justo después de la comilla de apertura
  EndIdx := Pos('"', Copy(Contents, Start, MaxInt));
  if EndIdx = 0 then Exit;
  Result := Copy(Contents, Start, EndIdx - 1);
end;

procedure WriteEnvFiles();
var
  EnvRoot, EnvDocker, EnvDockerExample: string;
  CredentialsDest, AllowedOrigins, Lines: string;
  Version: string;
  ExampleContents: AnsiString;
  ExampleAsString: string;
begin
  Version := '{#MyAppVersion}';

  CredentialsDest := ExpandConstant('{app}\sistema-de-asientos-automatizado\credentials\service_account.json');
  ForceDirectories(ExtractFilePath(CredentialsDest));
  CopyFile(CredentialsFilePath, CredentialsDest, False);

  // Extraer project_id del JSON automáticamente
  ProjectId := ExtractProjectIdFromCredentials(CredentialsDest);

  AllowedOrigins := 'http://localhost:3003,http://interfaz-asientos:3000';

  // Token aleatorio para autenticar las llamadas del backend al HTTP API de Watchtower.
  // Se genera una sola vez en la instalación. Nunca sale del docker-compose interno.
  Randomize;

  // .env raíz — usado por docker-compose
  EnvRoot := ExpandConstant('{app}\.env');
  Lines :=
    '# Generado automaticamente por aiwaf-setup' + #13#10 +
    'CREDENTIALS_PATH=' + CredentialsDest + #13#10 +
    'ALLOWED_ORIGINS=' + AllowedOrigins + #13#10 +
    'AIWAF_GESTORIA_NIF=' + GestoriaNif + #13#10 +
    'AIWAF_GESTORIA_NOMBRE=' + GestoriaNombre + #13#10 +
    'FEEDBACK_WEBHOOK_URL=' + WebhookUrl + #13#10 +
    'AIWAF_LATEST_VERSION_URL=https://aiwaf-releases.pages.dev/latest.json' + #13#10 +
    'WATCHTOWER_HTTP_API_TOKEN=' + GenerateRandomHex(32) + #13#10;
  SaveStringToFile(EnvRoot, Lines, False);

  // .env.docker — copia desde .env.docker.example (ya copiado por [Files])
  // sustituyendo el placeholder del project_id por el real extraido del JSON.
  // Esto garantiza que TODAS las variables que pydantic-settings exige se
  // propaguen automaticamente cuando el .example evolucione, sin tocar el .iss.
  EnvDockerExample := ExpandConstant('{app}\sistema-de-asientos-automatizado\.env.docker.example');
  EnvDocker := ExpandConstant('{app}\sistema-de-asientos-automatizado\.env.docker');
  if not LoadStringFromFile(EnvDockerExample, ExampleContents) then begin
    MsgBox('No se encontro .env.docker.example en la instalacion. ' +
           'El paquete del instalador esta corrupto; reconstruye el .exe y reintenta.',
           mbError, MB_OK);
    Abort;
  end;
  ExampleAsString := ExampleContents;
  StringChangeEx(ExampleAsString,
                 'GOOGLE_CLOUD_PROJECT_ID=tu-project-id-de-gcp',
                 'GOOGLE_CLOUD_PROJECT_ID=' + ProjectId, True);
  SaveStringToFile(EnvDocker, ExampleAsString, False);
end;

// ============================================================================
// Arranque inicial de contenedores con barra de progreso visible
// ============================================================================

function RunPowerShellSilent(const Cmd: string): Integer;
var
  ResultCode: Integer;
begin
  // Ejecuta un comando PowerShell oculto, devuelve exit code (0 = éxito)
  if not Exec('powershell.exe',
        '-NoProfile -ExecutionPolicy Bypass -Command "' + Cmd + '"',
        ExpandConstant('{app}'), SW_HIDE, ewWaitUntilTerminated, ResultCode) then
    Result := -1
  else
    Result := ResultCode;
end;

function IsDockerDaemonUp(): Boolean;
begin
  Result := RunPowerShellSilent('docker info 2>$null | Out-Null; if ($LASTEXITCODE -ne 0) { exit 1 } else { exit 0 }') = 0;
end;

function IsApiHealthy(): Boolean;
begin
  // Devuelve True si http://localhost:8003/health responde 200
  Result := RunPowerShellSilent(
    'try { $r = Invoke-WebRequest -Uri http://localhost:8003/health -UseBasicParsing -TimeoutSec 3; if ($r.StatusCode -eq 200) { exit 0 } else { exit 1 } } catch { exit 1 }') = 0;
end;

function IsInterfazUp(): Boolean;
begin
  Result := RunPowerShellSilent(
    'try { $r = Invoke-WebRequest -Uri http://localhost:3003 -UseBasicParsing -TimeoutSec 3; if ($r.StatusCode -lt 500) { exit 0 } else { exit 1 } } catch { exit 1 }') = 0;
end;

procedure TryStartDockerDesktop();
var
  ResultCode: Integer;
  DockerExe: string;
begin
  DockerExe := ExpandConstant('{pf}\Docker\Docker\Docker Desktop.exe');
  if FileExists(DockerExe) then
    Exec(DockerExe, '', '', SW_SHOWNORMAL, ewNoWait, ResultCode);
end;

function WaitForDockerDaemon(MaxSeconds: Integer): Boolean;
var
  i: Integer;
begin
  Result := False;
  for i := 1 to MaxSeconds do begin
    if IsDockerDaemonUp() then begin
      Result := True;
      Exit;
    end;
    Sleep(1000);
  end;
end;

procedure RunFirstStart();
var
  Page: TOutputProgressWizardPage;
  ResultCode, i: Integer;
  AppDir, LogPath, ComposeCmd: string;
  ApiHealthy, InterfazHealthy: Boolean;
begin
  AppDir   := ExpandConstant('{app}');
  LogPath  := AppDir + '\sistema-de-asientos-automatizado\logs\install.log';
  ForceDirectories(ExtractFilePath(LogPath));

  Page := CreateOutputProgressPage(
    'Iniciando AIWAF',
    'Configurando los servicios. La primera vez puede tardar 5–10 minutos ' +
    '(descarga y construcción de imágenes Docker). Puedes seguir trabajando ' +
    'con otras aplicaciones mientras tanto.');
  Page.Show;
  try
    // ---- Paso 1: Docker daemon disponible ----
    Page.SetText('Comprobando Docker Desktop...', '');
    Page.SetProgress(2, 100);
    if not IsDockerDaemonUp() then begin
      Page.SetText('Arrancando Docker Desktop...', '');
      TryStartDockerDesktop();
      if not WaitForDockerDaemon(120) then begin
        MsgBox('Docker Desktop no respondió en 2 minutos.' + #13#10 + #13#10 +
               'Asegúrate de que Docker Desktop está instalado y arrancado ' +
               '(icono de la ballena verde en la barra de tareas), y vuelve a ' +
               'ejecutar el instalador.', mbError, MB_OK);
        Abort;
      end;
    end;
    Page.SetProgress(10, 100);

    // ---- Paso 2: Pull de imágenes (puede fallar silenciosamente si no están en GHCR; OK) ----
    Page.SetText('Descargando imágenes Docker (puede tardar varios minutos)...', '');
    Page.SetProgress(15, 100);
    ComposeCmd := 'cd ''' + AppDir + '''; docker compose pull *>> ''' + LogPath + '''';
    RunPowerShellSilent(ComposeCmd);
    Page.SetProgress(40, 100);

    // ---- Paso 3: docker compose up -d --build ----
    Page.SetText('Construyendo e iniciando contenedores...', '');
    Page.SetProgress(45, 100);
    ComposeCmd := 'cd ''' + AppDir + '''; docker compose up -d --build *>> ''' + LogPath + '''; exit $LASTEXITCODE';
    ResultCode := RunPowerShellSilent(ComposeCmd);
    if ResultCode <> 0 then begin
      MsgBox('Falló al iniciar los contenedores Docker.' + #13#10 + #13#10 +
             'Revisa el log de instalación:' + #13#10 + LogPath + #13#10 + #13#10 +
             'Causas habituales:' + #13#10 +
             '  - Docker Desktop aún no está totalmente arrancado.' + #13#10 +
             '  - Conflicto de puertos (3003 u 8003 ocupados por otra app).' + #13#10 +
             '  - Falta de espacio en disco para imágenes Docker.', mbError, MB_OK);
      Abort;
    end;
    Page.SetProgress(60, 100);

    // ---- Paso 4: Esperar a que la API y la interfaz respondan ----
    Page.SetText('Esperando a que el sistema esté listo...', '');
    ApiHealthy      := False;
    InterfazHealthy := False;
    // Hasta 8 min adicionales (96 * 5s) para que el frontend Next.js compile en primer arranque
    for i := 1 to 96 do begin
      Page.SetProgress(60 + (i * 38) div 96, 100);
      if not ApiHealthy then ApiHealthy := IsApiHealthy();
      if ApiHealthy and (not InterfazHealthy) then InterfazHealthy := IsInterfazUp();
      if ApiHealthy and InterfazHealthy then Break;
      if (i mod 6) = 0 then begin
        if not ApiHealthy then
          Page.SetText('Esperando a que la API responda en localhost:8003...', '')
        else
          Page.SetText('API lista. Esperando a la interfaz en localhost:3003...', '');
      end;
      Sleep(5000);
    end;

    if not (ApiHealthy and InterfazHealthy) then begin
      MsgBox(
        'Los servicios no respondieron a tiempo tras la instalación.' + #13#10 + #13#10 +
        'API (8003): '      + IntToStr(Ord(ApiHealthy))      + ' (1=responde, 0=no)' + #13#10 +
        'Interfaz (3003): ' + IntToStr(Ord(InterfazHealthy)) + ' (1=responde, 0=no)' + #13#10 + #13#10 +
        'Los contenedores siguen iniciándose en segundo plano. Revisa el log:' + #13#10 +
        LogPath + #13#10 + #13#10 +
        'Si el problema persiste, ejecuta en una terminal:' + #13#10 +
        '  cd C:\AIWAF' + #13#10 +
        '  docker compose logs',
        mbInformation, MB_OK);
    end;

    // ---- Paso 5: Registrar autostart Windows ----
    Page.SetText('Configurando inicio automático con Windows...', '');
    Page.SetProgress(99, 100);
    RunPowerShellSilent(
      '& ''' + AppDir + '\installer\launcher.ps1'' -Action register-autostart');
    Page.SetProgress(100, 100);

    Page.SetText('AIWAF está listo. Pulsa Siguiente para abrir la interfaz.', '');
    Sleep(800);
  finally
    Page.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then begin
    // El webhook lo embebe Bruno al construir el .exe (variable de entorno
    // AIWAF_FEEDBACK_WEBHOOK definida en build.ps1, sustituida durante compile).
    // Si está vacía, el feedback queda solo en disco local.
    WebhookUrl := '{#GetEnv("AIWAF_FEEDBACK_WEBHOOK")}';
    WriteEnvFiles();
    RunFirstStart();
  end;
end;
