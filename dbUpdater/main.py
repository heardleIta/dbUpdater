
import json
import logging
import os
import re

import requests
from ytmusicapi import YTMusic

from . import ARTISTI_REVISIONATI
from .send import sender

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# Client YouTube Music usato per tutte le chiamate all'API di YTMusic
yt = YTMusic()


def getAllArtists():
    """Recupera tutti gli artisti presenti nel database dell'applicazione."""
    response = requests.get("https://be.heardleitalia.com/api/heardle/artist/all")
    response = response.json()
    if response.get("data") is None:
        log.error("Errore nella richiesta degli artisti: %s", response.get("errorMessage", response))
        return []
    return response["data"]["artists"]


def getAllAlbumOfArtistInDB(artistId):
    """Recupera dal DB tutti gli album già salvati per un dato artista (per ID YouTube)."""
    response = requests.get(f"https://be.heardleitalia.com/api/heardle/album?youtubeArtistId={artistId}")
    data = response.json()
    if data.get("data") is None:
        if data.get("errorMessage") != "Artist not found":
            log.error("Errore album per artista %s: %s", artistId, data.get("errorMessage", "Unknown error"))
        return []
    return data["data"]["albums"]


def getAllAlbumArtistsInYouTube(artistIdYouTube):
    """
    Recupera da YouTube Music la lista di album di un artista.
    Gestisce due casi restituiti dall'API:
      - album con params+browseId → richiede una chiamata aggiuntiva get_artist_albums
      - album già presenti nei risultati diretti
    Normalizza inoltre la chiave 'audioPlaylistId' → 'playlistId' per uniformità.
    """
    artista_obj = yt.get_artist(channelId=artistIdYouTube)
    albums = artista_obj.get("albums")
    listaAlbumArtista = []
    if albums is not None:
        params = albums.get("params")
        browseID = albums.get("browseId")
        if params and browseID:
            # L'artista ha molti album: serve una chiamata dedicata per ottenerli tutti
            listaAlbumArtista = yt.get_artist_albums(channelId=browseID, params=params)
        else:
            # Gli album sono già inclusi nella risposta principale
            listaAlbumArtista = albums.get("results", [])
    else:
        log.warning("Nessun album trovato per l'artista %s", artista_obj.get("name", artistIdYouTube))

    # Normalizza la chiave: alcuni album usano 'audioPlaylistId' invece di 'playlistId'
    for item in listaAlbumArtista:
        if "playlistId" not in item and "audioPlaylistId" in item:
            item["playlistId"] = item["audioPlaylistId"]
    return listaAlbumArtista


def filtraFeaturing(featuring, idArtista):
    """
    Rimuove dalla lista dei featuring l'artista principale e le voci senza ID.
    Restituisce [] se la lista risultante è vuota.
    """
    featuring = [a for a in featuring if a["id"] != idArtista and a["id"] is not None]
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
    if not any(author["youtubeAuthorId"] == artist["youtubeAuthorId"] for artist in newKeys):
        newKeys.append({
            "name": author["name"],
            "youtubeAuthorId": author["youtubeAuthorId"],
        })
    return newKeys


def getThumbnail(thumbnails):
    """Restituisce l'URL della thumbnail con la risoluzione più alta tra quelle disponibili."""
    if not thumbnails:
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
    for song in album_obj["tracks"]:
        # Salta tracce non riproducibili (es. video non disponibili)
        if song["videoId"] is None:
            continue
        # Salta versioni alternative che non vogliamo indicizzare
        title_lower = song["title"].lower()
        if re.search(r"\blive\b|\bremix\b|\bremastered\b", title_lower):
            continue

        newSong = {
            "title": song["title"],
            "duration": song["duration_seconds"],
            "youtubeSongId": song["videoId"],
            "youtubeViews": 0,
            # Se l'anno non è disponibile, usa 9999 come valore sentinella
            "releaseDate": album_obj.get("year", 9999) if album_obj.get("year", "") != "" else 9999,
            "artists": getAllArtistsInSongs(
                song["artists"],
                author={"name": artistName, "youtubeAuthorId": artistaChannelId},
            ),
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


def writeJSON(obj, filename):
    """Salva l'oggetto come file JSON nella cartella ArtistiRevisionati, pronto per l'invio."""
    os.makedirs(ARTISTI_REVISIONATI, exist_ok=True)
    path = os.path.join(ARTISTI_REVISIONATI, filename)
    try:
        with open(path, "w", encoding="utf-8") as outfile:
            json.dump(obj, outfile, ensure_ascii=False, indent=4)
    except Exception as e:
        log.error("Errore scrittura su %s: %s", filename, e)


if __name__ == "__main__":
    # Carica gli artisti dal DB e unisce quelli nuovi definiti localmente in newArtists.json
    allArtists = getAllArtists()
    newArtists_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "newArtists.json")
    newArtists = json.load(open(newArtists_path, "r", encoding="utf-8"))
    newArtistIds = {a["youtubeArtistId"] for a in newArtists}
    allArtists = newArtists + [a for a in allArtists if a["youtubeArtistId"] not in newArtistIds]

    log.info("Avvio elaborazione: %d artisti totali (%d nuovi)", len(allArtists), len(newArtists))

    for index, artist in enumerate(allArtists):
        allSongs = []
        log.info("[%d/%d] Artista: %s", index + 1, len(allArtists), artist["name"])
        try:
            # Recupera gli album dell'artista sia da YouTube che dal DB
            allAlbumYoutube = getAllAlbumArtistsInYouTube(artist["youtubeArtistId"])
            allAlbumDB = getAllAlbumOfArtistInDB(artist["youtubeArtistId"])

            if len(allAlbumYoutube) == 0:
                log.warning("Artista %s: nessun album trovato su YouTube", artist["name"])
                continue

            # Trova gli album presenti su YouTube ma non ancora nel DB (da aggiungere)
            albumDBIds = {album["youtubeAlbumId"] for album in allAlbumDB}
            filteredYTAlbums = [
                a for a in allAlbumYoutube
                if a.get("playlistId") is not None and a.get("playlistId") not in albumDBIds
            ]

            log.info(
                "Artista %s: %d album su YouTube, %d già nel DB, %d nuovi",
                artist["name"], len(allAlbumYoutube), len(allAlbumDB), len(filteredYTAlbums),
            )
            if filteredYTAlbums:
                for albumYTFiltered in filteredYTAlbums:
                    songs = getSongsOfAlbum(
                        albumYTFiltered["browseId"],
                        artistName=artist["name"],
                        artistaChannelId=artist["youtubeArtistId"],
                        idAlbum=albumYTFiltered["playlistId"],
                    )
                    log.info(
                        "  Album '%s' (%s): %d canzoni trovate",
                        albumYTFiltered.get("title", "?"), albumYTFiltered["playlistId"], len(songs),
                    )
                    allSongs += songs
                if allSongs:
                    writeJSON(allSongs, f"{artist['name']}.json")
                    log.info("Artista %s: %d canzoni totali salvate nel JSON", artist["name"], len(allSongs))
                else:
                    log.warning("Artista %s: album trovati ma nessuna canzone valida (tutte saltate?)", artist["name"])
            else:
                log.info("Artista %s: nessun album nuovo, skip", artist["name"])

        except Exception as e:
            log.error("Errore per l'artista %s (id: %s): %s", artist["name"], artist["youtubeArtistId"], e)

    log.info("Elaborazione completata. Avvio invio al backend...")
    # Invia al backend tutti i file JSON generati nella cartella ArtistiRevisionati
    sender()
