FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    alsa-utils \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir flask numpy google-api-python-client google-auth-oauthlib

WORKDIR /app
COPY app.py .
COPY templates/ templates/

# config.json se monta como volumen para que los cambios persistan
EXPOSE 5000

CMD ["python3", "app.py"]
