FROM python:3.11-slim

# Forza stdout/stderr non bufferizzati → i log appaiono subito in docker logs
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN pip install --no-cache-dir requests ytmusicapi

COPY . /app/dbUpdater/

RUN mkdir -p /app/ArtistiRevisionati /app/ArtistiSongSent

CMD ["python", "-m", "dbUpdater.main"]
