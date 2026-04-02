
import json
import os
import time
import shutil
import requests

ENDPOINT = "https://be.heardleitalia.com/api"

def sendArtist(artista, key):
  """
  Legge il file JSON dell'artista e invia le canzoni al backend tramite POST.
  In caso di successo sposta il file in ArtistiSongSent (così non viene reinviato).
  Restituisce (durata_richiesta_secondi, numero_canzoni_inviate).
  """
  with open("./ArtistiRevisionati/"+artista, "r", encoding="utf-8") as file:
    data = json.load(file)
    length = len(data)

    start_time = time.time()
    response = requests.post(
      f'{ENDPOINT}/heardle/insert/song',
      headers={
        'Authorization': f'Bearer {key}',
        'Content-Type': 'application/json',
        'x-api-key': 'key'
      },
      json=data
    )
    end_time = time.time()
    duration = end_time - start_time

    if(response.status_code == 200):
      file.close()
      # Crea la cartella di archivio se non esiste ancora
      if os.path.exists("./ArtistiSongSent/") == False:
        os.mkdir("./ArtistiSongSent/")
      # Sposta il file per evitare reinvii nelle esecuzioni successive
      shutil.move("./ArtistiRevisionati/"+artista, "./ArtistiSongSent/"+artista)
    else:
      print(f"Errore invio {artista}: HTTP {response.status_code}")
      try:
        print(f"  Response body: {response.json()}")
      except Exception:
        print(f"  Response body (raw): {response.text[:500]}")

  return duration, length


def sender():
  """
  Itera su tutti i file JSON in ArtistiRevisionati e li invia al backend.
  Per ogni artista ottiene prima un token fresco tramite /refresh,
  poi chiama sendArtist e stampa statistiche di avanzamento.
  """
  artistList = os.listdir("./ArtistiRevisionati/")
  totaleDuration = 0
  totaleCanzoniInviate = 0

  for index, artista in enumerate(artistList):
    # Ottieni un token di autenticazione aggiornato prima di ogni invio
    response = requests.get(f'{ENDPOINT}/refresh')
    if response.status_code != 200:
      print(f"Errore refresh token: HTTP {response.status_code} - {response.text[:200]}")
      continue
    response_data = response.json()
    key = response_data["data"]

    print(f"------------------------------------------------------------------------------------")
    print(f'{index} of {len(artistList)} - {artista}')

    duration, canzoniInviate = sendArtist(artista, key=key)
    totaleDuration += duration
    totaleCanzoniInviate += canzoniInviate

    print(f'Canzoni inserite per {artista} n: {canzoniInviate}')
    print(f'Tempo impiegato per {artista} n: {duration:.2f} secondi')
    print(f"Tempo totale: {totaleDuration:.2f} secondi - Canzoni inviate: {totaleCanzoniInviate}")
    print(f"Tempo medio: {totaleDuration / totaleCanzoniInviate:.2f} secondi per canzone")
