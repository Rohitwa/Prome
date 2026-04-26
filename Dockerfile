FROM python:3.12-slim

WORKDIR /app

# Deps first so layer caches survive code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# App code — `.dockerignore` excludes .git, venvs, secrets, sqlite files, etc.
# Using `COPY . .` so any new top-level module (db.py, auth.py, future helpers)
# gets included automatically without editing the Dockerfile.
COPY . .

# DBs live on a mounted volume so they survive image rebuilds.
ENV PROMEM_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8888

CMD ["python", "-m", "uvicorn", "promem_app:app", "--host", "0.0.0.0", "--port", "8888"]
