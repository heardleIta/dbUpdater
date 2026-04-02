
import json
import os
import requests
from ytmusicapi import YTMusic

from .send import sender
# Client YouTube Music usato per tutte le chiamate all'API di YTMusic
yt = YTMusic()


def getAllArtists():
  """Recupera tutti gli artisti presenti nel database dell'applicazione."""
  response = requests.get("https://be.heardleitalia.com/api/heardle/artist/all")
  response = response.json()
  return response['data']['artists']


def getAllAlbumOfArtistInDB(artistId):
  """Recupera dal DB tutti gli album già salvati per un dato artista (per ID YouTube)."""
  response = requests.get(f"https://be.heardleitalia.com/api/heardle/album?youtubeArtistId="+artistId)
  response = response.json()
  if response.get('data') is None:
    print(f"Errore nella richiesta degli album per l'artista {artistId}: {response.get('errorMessage', 'Unknown error')}")
    return []
  return response['data']['albums']

def getAllAlbumArtistsInYouTube(artistIdYouTube):
  """
  Recupera da YouTube Music la lista di album di un artista.
  Gestisce due casi restituiti dall'API:
    - album con params+browseId → richiede una chiamata aggiuntiva get_artist_albums
    - album già presenti nei risultati diretti
  Normalizza inoltre la chiave 'audioPlaylistId' → 'playlistId' per uniformità.
  """
  artista_obj = yt.get_artist(channelId=artistIdYouTube)
  albums = artista_obj.get('albums')
  listaAlbumArtista = {}
  if albums is not None:
          params = albums.get('params')
          browseID = albums.get('browseId')
          if params and browseID:
              # L'artista ha molti album: serve una chiamata dedicata per ottenerli tutti
              listaAlbumArtista = yt.get_artist_albums(channelId=browseID, params=params)
          else:
              # Gli album sono già inclusi nella risposta principale
              listaAlbumArtista = albums.get('results')
  else:
    print(f"Nessun album trovato per l'artista {artista_obj['name']}")

  # Normalizza la chiave: alcuni album usano 'audioPlaylistId' invece di 'playlistId'
  for item in listaAlbumArtista:
    if 'playlistId' not in item and 'audioPlaylistId' in item:
        item['playlistId'] = item['audioPlaylistId']
  return listaAlbumArtista

def filtraFeaturing(featuring, idArtista):
  """
  Rimuove dalla lista dei featuring l'artista principale e le voci senza ID.
  Restituisce [] se la lista risultante è vuota.
  """
  for artist in featuring:
    if artist['id'] == idArtista or artist['id'] is None:
      featuring.remove(artist)
  if len(featuring) == 0 or featuring is None:
    return []
  return featuring


def getAllArtistsInSongs(listArtists, author):
  """
  Costruisce la lista degli artisti di una canzone nel formato atteso dal DB.
  Aggiunge l'artista principale (author) se non è già presente tra gli artisti della traccia.
  """
  newKeys = []
  for artist in listArtists:
    newKeys.append({
      "name": artist["name"],
      "youtubeAuthorId": artist["id"],
    })
  # Assicura che l'artista principale dell'album sia sempre incluso
  if not any(author['youtubeAuthorId'] == artist['youtubeAuthorId'] for artist in newKeys):
    newKeys.append({
      "name": author["name"],
      "youtubeAuthorId": author["youtubeAuthorId"],
    })
  return newKeys

def getThumbnail(thumbnails):
  """Restituisce l'URL della thumbnail con la risoluzione più alta tra quelle disponibili."""
  if len(thumbnails) == 0:
    return None
  max_width = 0
  max_height = 0
  url = ""
  for thumbnail in thumbnails:
    if thumbnail["width"] > max_width and thumbnail["height"] > max_height:
      max_width = thumbnail["width"]
      max_height = thumbnail["height"]
      url = thumbnail["url"]
  return url

def getSongsOfAlbum(albumBrowseId, artistName, artistaChannelId, idAlbum):
  """
  Recupera le tracce di un album da YouTube Music e le formatta per il DB.
  Esclude tracce prive di videoId e tracce live, remix o remastered.
  """
  album_obj = yt.get_album(browseId=albumBrowseId)

  newSongs = []
  for song in album_obj['tracks']:
    # Salta tracce non riproducibili (es. video non disponibili)
    if(song['videoId'] is None):
      continue
    # Salta versioni alternative che non vogliamo indicizzare
    if("live" in song["title"].lower() or "remix" in song["title"].lower() or "remastered" in song["title"].lower()):
      continue

    newSong = {
            "title": song["title"],
            "duration": song["duration_seconds"],
            "youtubeSongId": song["videoId"],
            "youtubeViews": 0,
            # Se l'anno non è disponibile, usa 9999 come valore sentinella
            "releaseDate": album_obj.get("year", 9999) if album_obj.get("year", "") != "" else 9999,
            "artists": getAllArtistsInSongs(song["artists"], author={"name": artistName, "youtubeAuthorId": artistaChannelId}),
            "album": {
              "youtubeAlbumId": idAlbum,
              "thumbnail": getThumbnail(album_obj["thumbnails"]),
              "title": album_obj["title"],
              "releaseDate": album_obj.get("year", 9999) if album_obj.get("year", "") != "" else 9999,
              "author": {
                "name": artistName,
                "youtubeAuthorId": artistaChannelId,
              },
            },
          }
    newSongs.append(newSong)
  return newSongs

def writeJSON(obj, path):
  """Salva l'oggetto come file JSON nella cartella artistiRevisionati, pronto per l'invio."""
  try:
      with open(f'./artistiRevisionati/{path}', 'w', encoding='utf-8') as outfile:
          json.dump(obj, outfile, ensure_ascii=False, indent=4)
  except Exception as e:
    print(f'Errore scrittura su {path}: {e}')

if __name__ == '__main__':
  # Carica gli artisti dal DB e unisce quelli nuovi definiti localmente in newArtists.json
  allArtists = getAllArtists()
  newArtists = json.load(open(os.path.join(os.path.dirname(__file__), "newArtists.json"), "r", encoding="utf-8"))
  newArtistIds = {a['youtubeArtistId'] for a in newArtists}
  allArtists = newArtists + allArtists

  for index, artist in enumerate(allArtists):
    allSongs = []
    print(f"Artista {artist['name']} {index+1}/{len(allArtists)}")
    try:
      # Recupera gli album dell'artista sia da YouTube che dal DB
      allAlbumYoutube = getAllAlbumArtistsInYouTube(artist['youtubeArtistId'])
      isNew = artist['youtubeArtistId'] in newArtistIds
      allAlbumDB = [] if isNew else getAllAlbumOfArtistInDB(artist['youtubeArtistId'])

      if len(allAlbumYoutube) == 0:
        print(f"Artista {artist['name']} non presenta albums")
        continue

      # Trova gli album presenti su YouTube ma non ancora nel DB (da aggiungere)
      filteredYTAlbums = []
      albumDBIds = {album['youtubeAlbumId'] for album in allAlbumDB}
      for albumYT in allAlbumYoutube:
        if albumYT.get('playlistId') is not None and albumYT.get('playlistId') not in albumDBIds:
          filteredYTAlbums.append(albumYT)

      if len(filteredYTAlbums) > 0:
        print(f"Artista {artist['name']} ha {len(filteredYTAlbums)} album da aggiungere")
        for albumYTFiltered in filteredYTAlbums:
          # Recupera le canzoni del singolo album e le accumula
          allSongs = allSongs + getSongsOfAlbum(albumYTFiltered['browseId'], artistName=artist['name'], artistaChannelId=artist['youtubeArtistId'], idAlbum=albumYTFiltered['playlistId'])
          if len(allSongs) > 0:
            writeJSON(allSongs, f"{artist['name']}.json")
      else:
        print(f"Artista {artist['name']} non ha album da aggiungere")

    except Exception as e:
      print(f"Errore per l'artista {artist['name']} (id: {artist['youtubeArtistId']}): {e}")
      

  # Invia al backend tutti i file JSON generati nella cartella artistiRevisionati
  sender()
