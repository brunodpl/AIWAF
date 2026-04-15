# Guía de Instalación — Sistema de Asientos Automatizado HORECA

**Versión para técnico instalador (SPI Galicia)**

---

## ¿Qué hace este sistema?

Automatiza la creación de asientos contables a partir de facturas de clientes del sector HORECA. El usuario sube las facturas en PDF, el sistema las procesa con inteligencia artificial y genera un CSV listo para importar en Intermega.

---

## Requisitos previos

Antes de instalar, asegúrate de tener:

| Requisito | Mínimo | Notas |
|---|---|---|
| Sistema operativo | Windows 10/11 (64-bit) o Linux Ubuntu 20.04+ | — |
| RAM | 4 GB | 8 GB recomendado |
| Espacio en disco | 10 GB libres | Para imágenes Docker e datos |
| Conexión a internet | Sí | Para las llamadas a Google Cloud |
| Docker Desktop | Última versión | Ver instrucciones abajo |
| Archivo de credenciales GCP | `service_account.json` | Proporcionado por el desarrollador |
| Google Cloud Project ID | — | Proporcionado por el desarrollador |

---

## Paso 1 — Instalar Docker Desktop

### Windows

1. Descarga Docker Desktop: https://www.docker.com/products/docker-desktop/
2. Ejecuta el instalador y sigue el asistente
3. Reinicia el PC cuando se solicite
4. Abre Docker Desktop y espera a que el icono de la ballena esté verde

**Alternativa con PowerShell (Windows 10/11):**
```powershell
winget install Docker.DockerDesktop
```

### Linux (Ubuntu/Debian)

```bash
curl -fsSL https://get.docker.com | bash
sudo usermod -aG docker $USER
# Cierra sesión y vuelve a entrar para aplicar el grupo
```

---

## Paso 2 — Preparar los ficheros del sistema

1. Copia la carpeta del proyecto al equipo (USB, red local o descarga)
2. Crea la carpeta `sistema-de-asientos-automatizado\credentials\` si no existe
3. Pega el archivo `service_account.json` (proporcionado por Bruno) dentro de esa carpeta

Estructura esperada:
```
despliegue-proyecto-horeca\
  sistema-de-asientos-automatizado\
    credentials\
      service_account.json   ← aquí
  setup.ps1
  setup.sh
  ...
```

---

## Paso 3 — Ejecutar el instalador

### Windows

Abre PowerShell en la carpeta del proyecto y ejecuta:

```powershell
.\setup.ps1
```

El instalador te pedirá:
- **Google Cloud Project ID** (proporcionado por Bruno, ej: `mi-proyecto-gcp`)
- **Región Vertex AI** (pulsa Enter para usar `europe-west1` por defecto)

Tiempo estimado: 5-10 minutos la primera vez.

### Linux / Mac

```bash
bash setup.sh
```

El proceso es idéntico.

---

## Paso 4 — Verificar la instalación

Una vez completado el instalador, ejecuta el verificador:

```powershell
# Windows:
.\verify.ps1

# Linux/Mac:
bash verify.sh
```

Resultado esperado:
```
[OK]   API activa en http://localhost:8000
[OK]   Interfaz activa en http://localhost:3000
[OK]   Portainer activo en http://localhost:9000
[OK]   Credenciales GCP encontradas
[OK]   Google Cloud Project ID configurado
[OK]   Carpetas de trabajo creadas
[WARN] data/clients.json está vacío — añade clientes antes de procesar facturas
```

El aviso sobre `clients.json` es normal en una instalación nueva. Ver Paso 5.

---

## Paso 5 — Configurar los clientes de la gestoría

Antes de procesar facturas, el sistema necesita saber qué clientes tiene la gestoría y qué cuenta contable les corresponde.

Edita el fichero `sistema-de-asientos-automatizado\data\clients.json`:

```json
[
  {
    "nif": "B12345678",
    "nombre": "Restaurante El Buen Gusto S.L.",
    "cuenta_contable": "430"
  },
  {
    "nif": "A87654321",
    "nombre": "Hotel Vista Mar S.A.",
    "cuenta_contable": "430"
  }
]
```

---

## Acceso al sistema

Una vez instalado, accede desde cualquier navegador:

| Servicio | URL | Para qué |
|---|---|---|
| Interfaz principal | http://localhost:3000 | Procesar facturas y exportar CSV |
| Gestión Docker | http://localhost:9000 | Ver estado, reiniciar, ver logs |
| API backend | http://localhost:8000 | Solo para diagnóstico técnico |

**Para acceder desde otro PC de la red local**, reemplaza `localhost` por la IP del servidor donde está instalado (ej: `http://192.168.1.100:3000`).

---

## Comandos del día a día

Desde PowerShell (Windows) o terminal (Linux), en la carpeta del proyecto:

```bash
# Iniciar el sistema (si se ha parado)
make start

# Parar el sistema
make stop

# Ver logs en tiempo real
make logs

# Estado de los contenedores
make status

# Verificar que todo funciona
.\verify.ps1   # Windows
bash verify.sh  # Linux
```

---

## Flujo de trabajo típico

1. **Subir facturas**: En la interfaz (http://localhost:3000), selecciona el libro contable (Compras y Gastos, Ventas, Bienes de Inversión) y sube los PDFs de las facturas.

2. **Procesar**: Haz clic en "Ejecutar pipeline". El sistema procesará las facturas automáticamente.

3. **Revisar**: Aparecerá la lista de facturas procesadas. Las marcadas en verde se han auto-validado; las amarillas o rojas requieren revisión manual del contable.

4. **Exportar**: En la pestaña "Exportar", descarga el CSV listo para importar en Intermega.

---

## Solución de problemas frecuentes

| Síntoma | Causa probable | Solución |
|---|---|---|
| La interfaz no carga | Contenedores parados | Ejecuta `make start` |
| "API no responde" en verify | Backend no arrancó | `docker compose logs pipeline-api` |
| Error de credenciales | service_account.json incorrecto o mal colocado | Verifica la ruta en `sistema-de-asientos-automatizado\credentials\` |
| Facturas no se procesan | `clients.json` vacío | Añade clientes (ver Paso 5) |
| Pipeline falla en OCR | Credenciales GCP sin permisos | La cuenta de servicio necesita Cloud Vision API User + Vertex AI User |
| Docker no arranca | Docker Desktop parado | Abre Docker Desktop y espera a que esté verde |

---

## Actualizar el sistema

Cuando haya una nueva versión:

```bash
# Descargar la nueva versión (el desarrollador indica cómo)
# Luego:
make rebuild
```

---

## Soporte

**Bruno** — desarrollador del sistema  
Para dudas técnicas o problemas de instalación.

---

*Sistema de Asientos Automatizado HORECA — Instalación on-premise vía Docker*
