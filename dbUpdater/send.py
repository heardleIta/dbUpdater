
import json
import logging
import os
import shutil
import time

import requests

from . import ARTISTI_REVISIONATI, ARTISTI_SENT

log = logging.getLogger(__name__)

ENDPOINT = "https://be.heardleitalia.com/api"


def sendArtist(artista, key):
    """
    Legge il file JSON dell'artista e invia le canzoni al backend tramite POST.
    In caso di successo sposta il file in ArtistiSongSent (così non viene reinviato).
    Restituisce (durata_richiesta_secondi, numero_canzoni_inviate).
    """
    filepath = os.path.join(ARTISTI_REVISIONATI, artista)
    with open(filepath, "r", encoding="utf-8") as file:
        data = json.load(file)
        length = len(data)

        start_time = time.time()
        response = requests.post(
            f"{ENDPOINT}/heardle/insert/song",
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "x-api-key": "key",
            },
            json=data,
        )
        duration = time.time() - start_time

        if response.status_code == 200:
            file.close()
            os.makedirs(ARTISTI_SENT, exist_ok=True)
            shutil.move(filepath, os.path.join(ARTISTI_SENT, artista))
        else:
            log.error("Errore invio %s: HTTP %d", artista, response.status_code)
            try:
                log.error("  Response: %s", response.json())
            except Exception:
                log.error("  Response (raw): %s", response.text[:500])

    return duration, length


def sender():
    """
    Itera su tutti i file JSON in ArtistiRevisionati e li invia al backend.
    Per ogni artista ottiene prima un token fresco tramite /refresh,
    poi chiama sendArtist e stampa statistiche di avanzamento.
    """
    if not os.path.isdir(ARTISTI_REVISIONATI):
        log.warning("Cartella ArtistiRevisionati non trovata, nessun file da inviare.")
        return

    artistList = [f for f in os.listdir(ARTISTI_REVISIONATI) if f.endswith(".json")]
    if not artistList:
        log.info("Nessun artista da inviare.")
        return

    log.info("Invio di %d artisti al backend...", len(artistList))
    totaleDuration = 0
    totaleCanzoniInviate = 0

    for index, artista in enumerate(artistList):
        response = requests.get(f"{ENDPOINT}/refresh")
        if response.status_code != 200:
            log.error("Errore refresh token: HTTP %d - %s", response.status_code, response.text[:200])
            continue
        key = response.json()["data"]

        log.info("[%d/%d] Invio: %s", index + 1, len(artistList), artista)
        duration, canzoniInviate = sendArtist(artista, key=key)
        totaleDuration += duration
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
