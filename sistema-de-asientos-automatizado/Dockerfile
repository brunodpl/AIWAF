FROM python:3.12-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements e instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar FastAPI y uvicorn para el API server
RUN pip install --no-cache-dir fastapi[standard] uvicorn

# Copiar código fuente
COPY src/ ./src/
COPY data/ ./data/

# Crear directorios necesarios y usuario no-root
RUN mkdir -p data/output logs/audit horeca_sandbox credentials \
    && groupadd -r app && useradd -r -g app -d /app -s /sbin/nologin app \
    && chown -R app:app /app

# Exponer puerto para la API
EXPOSE 8000

# Ejecutar como usuario no-root
USER app

# Comando por defecto: iniciar API server
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
