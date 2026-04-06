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

# Crear directorios necesarios
RUN mkdir -p data/output logs/audit horeca_sandbox

# Exponer puerto para la API
EXPOSE 8000

# Variable de entorno para las credenciales (se montará via volume)
ENV GOOGLE_APPLICATION_CREDENTIALS=/app/credentials/service_account.json

# Comando por defecto: iniciar API server
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
