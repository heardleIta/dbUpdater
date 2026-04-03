FROM python:3.11-slim

# Forza stdout/stderr non bufferizzati → i log appaiono subito in docker logs
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Installa le dipendenze prima di copiare il codice per sfruttare la cache dei layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dbUpdater/ ./dbUpdater/
COPY newArtists.json .

# Fallback se si usa docker run senza compose (con compose i bind mount hanno la precedenza)
RUN mkdir -p ArtistiRevisionati ArtistiSongSent

CMD ["python", "-m", "dbUpdater.main"]
