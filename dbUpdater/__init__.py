import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARTISTI_REVISIONATI = os.path.join(BASE_DIR, "ArtistiRevisionati")
ARTISTI_SENT = os.path.join(BASE_DIR, "ArtistiSongSent")

# Stato persistente: database del servizio di controllo, log ed eventi dei run,
# checkpoint dei testi. In Docker punta a un volume. Senza, il checkpoint dei
# testi finiva nel filesystem effimero del container e si perdeva a ogni
# ricreazione, facendo ri-scaricare da capo testi già inseriti.
DATA_DIR = os.environ.get("UPDATER_DATA_DIR", os.path.join(BASE_DIR, "data"))


def api_endpoint() -> str:
    """
    Base URL del backend Heardle, da `HEARDLE_API_URL`.

    Non esiste un valore di ripiego: prima era la produzione, quindi una env
    dimenticata (refuso nel .env, `docker run` senza --env-file, script lanciato
    a mano) faceva scrivere sul database vero senza alcun segnale. Meglio non
    partire affatto che scrivere nel posto sbagliato.
    """
    url = os.environ.get("HEARDLE_API_URL", "").strip()
    if not url:
        raise RuntimeError(
            "HEARDLE_API_URL non impostata: rifiuto di procedere senza sapere "
            "su quale backend scrivere. Valorizzarla nel .env (es. "
            "http://host.docker.internal:8080/api per l'ambiente locale)."
        )
    return url.rstrip("/")
