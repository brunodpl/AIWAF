# AIWAF

AIWAF convierte facturas recibidas en datos contables revisables y exportables a Intermega.

El proyecto está dirigido a gestorías que procesan facturas de empresas HORECA.

## Problema y solución

Registrar facturas manualmente consume tiempo y facilita errores en datos fiscales y contables.

El sistema extrae la información, comprueba reglas deterministas y separa los casos dudosos para revisión humana.

No contabiliza una factura por su cuenta ni sustituye la validación profesional.

## Proceso

1. La interfaz recibe facturas en PDF o imagen.
2. Google Cloud Vision extrae el texto y Gemini lo estructura cuando está configurado.
3. Los módulos validan identidades, importes e impuestos y clasifican el concepto contable.
4. Los casos incompletos o dudosos pasan a revisión en la interfaz.
5. Los datos aprobados se exportan para importarlos en Intermega.

## Diseño técnico

La interfaz usa Next.js y el backend expone una API FastAPI que coordina el pipeline Python.

Docker Compose conecta ambos servicios en una red privada y publica la interfaz solo en `127.0.0.1:3003`.

La API no publica un puerto en el host y rechaza peticiones de navegador con un origen externo.

Los documentos, resultados y registros permanecen en volúmenes locales configurados por el despliegue.

Google Cloud Vision y Vertex AI requieren credenciales locales; no se incluyen claves ni datos reales de clientes.

## Desarrollo

Se necesitan Docker Compose, Node.js con pnpm, Python 3.12 y credenciales de Google Cloud para el procesamiento real.

```powershell
Copy-Item .env.example .env
Copy-Item sistema-de-asientos-automatizado/.env.docker.example sistema-de-asientos-automatizado/.env.docker
pnpm --dir interfaz-asientos-automatizados install --frozen-lockfile
uv sync --project sistema-de-asientos-automatizado
docker compose -f docker-compose.yml -f docker-compose.dev.yml up --build pipeline-api interfaz
```

La interfaz queda disponible en `http://127.0.0.1:3003`.

Las pruebas offline del backend se ejecutan con `uv run --project sistema-de-asientos-automatizado pytest -q`.

Las pruebas de interfaz se ejecutan con `pnpm --dir interfaz-asientos-automatizados test`.

`pnpm test:e2e:multi` llama a servicios reales y limpia los directorios del volumen `libros/` al iniciar cada caso.

No se debe ejecutar esa suite contra una instalación con documentos que haya que conservar.

## Límites

La extracción depende de servicios de Google y puede requerir corrección manual cuando la factura es ilegible o ambigua.

El despliegue incluido es local para una persona y no incorpora autenticación multiusuario.

No se debe exponer Docker, la interfaz ni la API a una red pública.
