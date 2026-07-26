"""
API di controllo dell'updater, consumata dal back office di Heardle Italia.

Le risposte usano lo stesso involucro del backend Heardle
(`{errorCode, errorMessage, path, timestamp, data}`), così il frontend può
riusare `responseBaseSchema` senza casi particolari.
"""

import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .. import ARTISTI_REVISIONATI
from .. import ARTISTI_SENT as ARTISTI_SENT_LEGACY
from ..send import send_one
from .. import store
from . import catalog, runner
from .security import require_admin

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

@asynccontextmanager
async def lifespan(_: FastAPI):
    store.init()
    # Un run rimasto 'running' dopo un riavvio bloccherebbe per sempre il lock.
    runner.reconcile()
    # I JSON archiviati dal vecchio meccanismo diventano righe del recap, così
    # l'elenco degli artisti già inseriti non parte vuoto.
    imported = store.sent_import_archive(ARTISTI_SENT_LEGACY)
    if imported:
        log.info("Recap invii: importati %d artisti dall'archivio storico.", imported)
    log.info("Servizio di controllo pronto.")
    yield


app = FastAPI(
    title="Heardle dbUpdater — API di controllo",
    description="Coda artisti, esecuzione dei run di scraping e approvazione dei risultati.",
    version="1.0.0",
    lifespan=lifespan,
)

_origins = os.environ.get("UPDATER_CORS_ORIGINS", "*")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in _origins.split(",")] if _origins != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "PATCH", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


# ── Involucro di risposta ────────────────────────────────────────────────────


def envelope(request: Request, data):
    return {
        "errorCode": 0,
        "errorMessage": None,
        "path": request.url.path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "data": data,
    }


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "errorCode": exc.status_code,
            "errorMessage": exc.detail,
            "path": request.url.path,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": None,
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.exception("Errore non gestito su %s", request.url.path)
    return JSONResponse(
        status_code=500,
        content={
            "errorCode": 500,
            "errorMessage": str(exc)[:300] or "Errore interno",
            "path": request.url.path,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "data": None,
        },
    )


# ── Modelli ──────────────────────────────────────────────────────────────────


class ArtistIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    youtubeArtistId: str = Field(min_length=5, max_length=64)


class BulkArtistsIn(BaseModel):
    """
    Accetta sia una lista già strutturata sia il testo grezzo incollato nella
    textarea, così il back office può girare il JSON senza pre-elaborarlo.
    """

    artists: list[ArtistIn] | None = None
    raw: str | None = None


class RunIn(BaseModel):
    """
    Ambito del run:
      - `artists` valorizzato  → solo quegli artisti, niente altro
      - `onlyQueued=True`      → solo gli artisti in coda non ancora elaborati
      - `onlyQueued=False`     → coda + tutti gli artisti già nel database
    """

    mode: str = "full"
    onlyQueued: bool = True
    artists: list[str] | None = None
    autoSend: bool = False


class QuickRunIn(ArtistIn):
    """Artista singolo da accodare e aggiornare subito."""

    mode: str = "full"
    autoSend: bool = False


# ── Stato ────────────────────────────────────────────────────────────────────


@app.get("/health")
def health():
    """Sonda di liveness, senza autenticazione."""
    return {"status": "ok"}


@app.get("/status")
def get_status(request: Request, tor: bool = False, _: str = Depends(require_admin)):
    """
    Stato di sintesi per l'intestazione del back office. Il controllo dell'exit
    Tor è opzionale (`?tor=true`) perché richiede alcune query GeoIP che possono
    prendere qualche secondo.
    """
    active = store.run_active()
    queue = store.queue_list()
    data = {
        "activeRun": active,
        "lastRuns": store.run_list(limit=5),
        "queuePending": sum(1 for a in queue if not a["processed_at"]),
        "queueTotal": len(queue),
        "reviewPending": len(runner.review_files()),
        # Su quale backend finiranno i dati. Il pannello lo mostra sempre: senza
        # questa informazione non si distingue un ambiente di prova dalla produzione.
        "targetApi": catalog.ENDPOINT,
        "tor": None,
    }

    if tor:
        try:
            from ..main import _exit_verdict, _get_exit_geo

            ip, primary, secondaries = _get_exit_geo()
            ok, reason = _exit_verdict(primary, secondaries)
            # Il primario può non dare verdetto (ipinfo.io blocca Tor): si mostra
            # comunque un paese, preso dai secondari, mai `null`.
            country = primary or (secondaries[0] if secondaries else "n/d")
            data["tor"] = {
                "ip": ip,
                "country": country,
                "secondaries": secondaries,
                "ok": ok,
                "reason": reason,
            }
        except Exception as e:
            data["tor"] = {"error": str(e)[:200]}

    return envelope(request, data)


# ── Coda artisti ─────────────────────────────────────────────────────────────


@app.get("/artists/db")
def list_db_artists(request: Request, _: str = Depends(require_admin)):
    try:
        artists = catalog.db_artists()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Backend non raggiungibile: {e}")
    return envelope(request, {"artists": artists})


@app.get("/artists/queue")
def list_queue(request: Request, _: str = Depends(require_admin)):
    return envelope(request, {"queue": store.queue_list()})


@app.post("/artists/queue")
def add_to_queue(request: Request, artist: ArtistIn, _: str = Depends(require_admin)):
    outcome = store.queue_add(artist.name.strip(), artist.youtubeArtistId.strip())
    if outcome == "duplicate":
        raise HTTPException(status_code=409, detail="Artista già in coda")
    return envelope(request, {"queue": store.queue_list()})


@app.post("/artists/queue/bulk")
def add_bulk_to_queue(request: Request, payload: BulkArtistsIn, _: str = Depends(require_admin)):
    """
    Accoda un blocco di artisti nel formato storico di `newArtists.json`.
    Restituisce l'esito voce per voce: nulla viene scartato in silenzio.
    """
    items: list[dict] = []
    if payload.artists:
        items = [a.model_dump() for a in payload.artists]
    elif payload.raw:
        try:
            parsed = json.loads(payload.raw)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=422, detail=f"JSON non valido: {e}")
        if not isinstance(parsed, list):
            raise HTTPException(status_code=422, detail="Il JSON deve essere una lista di artisti")
        items = parsed
    else:
        raise HTTPException(status_code=422, detail="Nessun artista fornito")

    try:
        in_db = {a["youtubeArtistId"] for a in catalog.db_artists()}
    except Exception:
        # Il dedup contro il DB è un di più: se il backend non risponde
        # accodiamo comunque, lo scraping salterà gli album già presenti.
        in_db = set()

    added, duplicates, already, invalid = [], [], [], []
    for raw_item in items:
        if not isinstance(raw_item, dict):
            invalid.append({"item": str(raw_item)[:120], "reason": "voce non valida"})
            continue
        name = str(raw_item.get("name") or "").strip()
        yt_id = str(raw_item.get("youtubeArtistId") or "").strip()
        if not name or not yt_id:
            invalid.append({"item": str(raw_item)[:120], "reason": "name o youtubeArtistId mancante"})
            continue
        if " " in yt_id or len(yt_id) < 10:
            invalid.append({"item": name, "reason": f"id YouTube sospetto: {yt_id}"})
            continue
        if yt_id in in_db:
            already.append({"name": name, "youtubeArtistId": yt_id})
            continue
        if store.queue_add(name, yt_id) == "added":
            added.append({"name": name, "youtubeArtistId": yt_id})
        else:
            duplicates.append({"name": name, "youtubeArtistId": yt_id})

    return envelope(
        request,
        {
            "added": added,
            "duplicates": duplicates,
            "alreadyInDb": already,
            "invalid": invalid,
            "queue": store.queue_list(),
        },
    )


@app.delete("/artists/queue/processed")
def remove_processed_from_queue(request: Request, _: str = Depends(require_admin)):
    """
    Svuota le voci già elaborate. Dichiarata prima della rotta parametrica,
    altrimenti "processed" verrebbe letto come id.
    """
    removed = store.queue_remove_processed()
    return envelope(request, {"removed": removed, "queue": store.queue_list()})


@app.delete("/artists/queue/{queue_id}")
def remove_from_queue(request: Request, queue_id: int, _: str = Depends(require_admin)):
    if not store.queue_remove(queue_id):
        raise HTTPException(status_code=404, detail="Voce non trovata in coda")
    return envelope(request, {"queue": store.queue_list()})


# ── Run ──────────────────────────────────────────────────────────────────────


@app.post("/runs")
def start_run(request: Request, payload: RunIn, _: str = Depends(require_admin)):
    try:
        run_id = runner.start(
            mode=payload.mode,
            only_queued=payload.onlyQueued,
            artists=payload.artists,
            auto_send=payload.autoSend,
        )
    except runner.RunnerError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return envelope(request, {"run": store.run_get(run_id)})


@app.post("/artists/quick-run")
def quick_run(request: Request, payload: QuickRunIn, _: str = Depends(require_admin)):
    """
    Aggiunge un artista (nome + channel id) e lancia subito un run limitato a
    lui: è il caso "mi manca questo artista" senza ripassare tutto il catalogo.
    Se l'artista è già in coda non viene duplicato, il run parte comunque.
    """
    name = payload.name.strip()
    yt_id = payload.youtubeArtistId.strip()
    store.queue_add(name, yt_id)

    try:
        run_id = runner.start(
            mode=payload.mode,
            only_queued=True,
            artists=[yt_id],
            auto_send=payload.autoSend,
        )
    except runner.RunnerError as e:
        raise HTTPException(status_code=409, detail=str(e))

    return envelope(
        request,
        {"run": store.run_get(run_id), "artist": {"name": name, "youtubeArtistId": yt_id}},
    )


@app.get("/runs")
def list_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    _: str = Depends(require_admin),
):
    return envelope(request, {"runs": store.run_list(limit)})


@app.get("/runs/{run_id}")
def get_run(request: Request, run_id: int, _: str = Depends(require_admin)):
    run = store.run_get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Run non trovato")
    return envelope(request, {"run": run})


@app.get("/runs/{run_id}/events")
def get_run_events(
    request: Request,
    run_id: int,
    cursor: int = Query(default=0, ge=0),
    _: str = Depends(require_admin),
):
    """Eventi comparsi dopo `cursor`: la UI ripassa il cursore che riceve."""
    if not store.run_get(run_id):
        raise HTTPException(status_code=404, detail="Run non trovato")
    events, next_cursor = runner.read_events(run_id, cursor)
    return envelope(
        request,
        {"events": events, "cursor": next_cursor, "run": store.run_get(run_id)},
    )


@app.get("/runs/{run_id}/logs")
def get_run_logs(
    request: Request,
    run_id: int,
    cursor: int = Query(default=0, ge=0),
    _: str = Depends(require_admin),
):
    if not store.run_get(run_id):
        raise HTTPException(status_code=404, detail="Run non trovato")
    text, next_cursor = runner.read_log(run_id, cursor)
    return envelope(request, {"text": text, "cursor": next_cursor})


@app.post("/runs/{run_id}/cancel")
def cancel_run(request: Request, run_id: int, _: str = Depends(require_admin)):
    if not runner.cancel(run_id):
        raise HTTPException(status_code=409, detail="Il run non è in corso")
    return envelope(request, {"run": store.run_get(run_id)})


# ── Coda di revisione ────────────────────────────────────────────────────────


def _safe_review_path(filename: str) -> str:
    """Impedisce che un nome di file esca dalla cartella di revisione."""
    name = os.path.basename(filename)
    if not name.endswith(".json") or name != filename:
        raise HTTPException(status_code=400, detail="Nome file non valido")
    path = os.path.join(ARTISTI_REVISIONATI, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File non trovato nella coda di revisione")
    return path


@app.get("/review")
def list_review(request: Request, _: str = Depends(require_admin)):
    return envelope(request, {"files": runner.review_files()})


# Va dichiarata PRIMA di /review/{filename}, altrimenti "sent" verrebbe preso
# come nome di file dalla rotta parametrica.
@app.get("/review/sent")
def list_sent(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    _: str = Depends(require_admin),
):
    """
    Gli artisti già approvati e accettati dal backend, dal più recente.

    È un resoconto (chi, quando, quante canzoni): del contenuto inviato non si
    conserva copia, il JSON viene cancellato appena il backend lo accetta.
    """
    return envelope(request, {"sent": store.sent_list(limit)})


@app.get("/review/{filename}")
def get_review_file(request: Request, filename: str, _: str = Depends(require_admin)):
    path = _safe_review_path(filename)
    with open(path, "r", encoding="utf-8") as f:
        songs = json.load(f)
    return envelope(request, {"file": filename, "songs": songs})


# Approvazioni in corso, per nome file. Due invii dello stesso artista in
# parallelo fanno gareggiare il backend su se stesso — l'inserimento degli
# artisti non è protetto contro le scritture concorrenti e uno dei due muore su
# violazione di chiave univoca, riportando un errore per un'operazione che in
# realtà è riuscita.
_approving: set[str] = set()
_approving_lock = threading.Lock()


@app.post("/review/{filename}/approve")
def approve_review_file(request: Request, filename: str, _: str = Depends(require_admin)):
    """
    Invia al backend le canzoni approvate e archivia il file.

    È idempotente: se il file è già stato archiviato l'inserimento era già
    andato a buon fine, e lo si dichiara invece di rispondere "non trovato".
    """
    name = os.path.basename(filename)
    if not name.endswith(".json") or name != filename:
        raise HTTPException(status_code=400, detail="Nome file non valido")

    with _approving_lock:
        if name in _approving:
            raise HTTPException(
                status_code=409,
                detail="Inserimento già in corso per questo artista",
            )

        in_review = os.path.isfile(os.path.join(ARTISTI_REVISIONATI, name))
        already_sent = store.sent_find(name)
        if not in_review:
            if already_sent:
                return envelope(
                    request,
                    {
                        "file": name,
                        "songs": already_sent.get("songs") or 0,
                        "duration": 0,
                        "alreadySent": True,
                        "files": runner.review_files(),
                    },
                )
            raise HTTPException(
                status_code=404, detail="File non trovato nella coda di revisione"
            )
        _approving.add(name)

    try:
        duration, songs, ok = send_one(name)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Invio fallito: {e}")
    finally:
        with _approving_lock:
            _approving.discard(name)

    if not ok:
        raise HTTPException(status_code=502, detail="Il backend ha rifiutato l'invio")
    return envelope(
        request,
        {
            "file": name,
            "songs": songs,
            "duration": round(duration, 2),
            "alreadySent": False,
            "files": runner.review_files(),
        },
    )



@app.delete("/review/{filename}")
def discard_review_file(request: Request, filename: str, _: str = Depends(require_admin)):
    path = _safe_review_path(filename)
    os.remove(path)
    log.info("Scartato dalla revisione: %s", filename)
    return envelope(request, {"files": runner.review_files()})
