
import json
import logging
import os
import time

import requests

from . import ARTISTI_REVISIONATI
from . import events
from . import store
from .auth import auth_headers
from . import api_endpoint

log = logging.getLogger(__name__)

ENDPOINT = api_endpoint()


def sendArtist(artista):
    """
    Legge il file JSON dell'artista e invia le canzoni al backend tramite POST.
    In caso di successo registra l'invio nel database e cancella il file, così
    non viene rispedito.
    Restituisce (durata_richiesta_secondi, numero_canzoni_inviate, ok).

    Se il backend risponde 401 il token viene rigenerato una volta e la richiesta
    ritentata: un run lungo può superare la scadenza del JWT a metà invio.
    """
    filepath = os.path.join(ARTISTI_REVISIONATI, artista)
    with open(filepath, "r", encoding="utf-8") as file:
        data = json.load(file)
    length = len(data)

    start_time = time.time()
    response = requests.post(
        f"{ENDPOINT}/heardle/insert/song", headers=auth_headers(), json=data
    )
    if response.status_code == 401:
        log.warning("Token scaduto durante l'invio di %s, rigenero e ritento", artista)
        response = requests.post(
            f"{ENDPOINT}/heardle/insert/song",
            headers=auth_headers(force_refresh=True),
            json=data,
        )
    duration = time.time() - start_time

    if response.status_code == 200:
        # Il JSON non viene archiviato: del contenuto non serve più nulla una
        # volta che il backend l'ha accettato, e un artista pesava anche mezzo
        # megabyte. Resta il resoconto dell'invio nel database.
        store.sent_add(
            artist=artista[:-5] if artista.endswith(".json") else artista,
            file=artista,
            songs=length,
            duration=round(duration, 2),
        )
        os.remove(filepath)
        log.info("  OK: %d canzoni inviate in %.2fs", length, duration)
        events.emit("send_ok", file=artista, songs=length, duration=round(duration, 2))
        return duration, length, True

    log.error("Errore invio %s: HTTP %d", artista, response.status_code)
    try:
        detail = response.json()
    except Exception:
        detail = response.text[:500]
    log.error("  Response: %s", detail)
    events.emit(
        "send_error", file=artista, songs=length, status=response.status_code, detail=str(detail)[:500]
    )
    return duration, length, False


def send_one(filename):
    """
    Invia un singolo file della coda di revisione. Usato dal back office quando
    l'operatore approva un artista, invece di spedire tutta la cartella.
    """
    if not os.path.isfile(os.path.join(ARTISTI_REVISIONATI, filename)):
        raise FileNotFoundError(f"{filename} non è nella coda di revisione")
    return sendArtist(filename)


def sender():
    """
    Itera su tutti i file JSON in ArtistiRevisionati e li invia al backend,
    stampando statistiche di avanzamento.
    """
    if not os.path.isdir(ARTISTI_REVISIONATI):
        log.warning("Cartella ArtistiRevisionati non trovata, nessun file da inviare.")
        return

    artistList = [f for f in os.listdir(ARTISTI_REVISIONATI) if f.endswith(".json")]
    if not artistList:
        log.info("Nessun artista da inviare.")
        return

    log.info("Invio di %d artisti al backend...", len(artistList))
    events.emit("send_start", files=len(artistList))
    totaleDuration = 0
    totaleCanzoniInviate = 0

    for index, artista in enumerate(artistList):
        log.info("[%d/%d] Invio: %s", index + 1, len(artistList), artista)
        try:
            duration, canzoniInviate, ok = sendArtist(artista)
        except Exception as e:
            log.error("Errore invio %s: %s", artista, e)
            events.emit("send_error", file=artista, detail=str(e)[:500])
            continue

        totaleDuration += duration
        if ok:
            totaleCanzoniInviate += canzoniInviate

        log.info(
            "  Canzoni: %d | Tempo: %.2fs | Totale: %d canzoni in %.2fs (media %.2fs/canzone)",
            canzoniInviate,
            duration,
            totaleCanzoniInviate,
            totaleDuration,
            totaleDuration / totaleCanzoniInviate if totaleCanzoniInviate else 0,
        )

    log.info("Invio completato: %d canzoni in %.2f secondi.", totaleCanzoniInviate, totaleDuration)
    events.emit("send_end", songs=totaleCanzoniInviate, duration=round(totaleDuration, 2))
