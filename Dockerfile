FROM python:3.12-slim

WORKDIR /app

# Deps first so layer caches survive code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code.
COPY promem_app.py promem_orchestrator.py ./
COPY promem_pipeline/ ./promem_pipeline/
COPY templates/ ./templates/
COPY migrations/ ./migrations/

# DBs live on a mounted volume so they survive image rebuilds.
ENV PROMEM_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8888

CMD ["python", "-m", "uvicorn", "promem_app:app", "--host", "0.0.0.0", "--port", "8888"]
