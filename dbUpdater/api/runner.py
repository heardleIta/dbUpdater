"""
Avvio e sorveglianza dei run di scraping.

Il run è un sottoprocesso `python -m dbUpdater.main`: la logica di scraping resta
quella che gira già a mano, e un riavvio del servizio di controllo non uccide un
run in corso (che può durare ore). Il sottoprocesso scrive i log su file e, via
`UPDATER_EVENTS_FILE`, gli eventi strutturati che la UI usa per l'avanzamento.

Si ammette un solo run alla volta: Tor e i rate limit di YouTube non reggono
richieste parallele, ed è proprio la fretta a far scattare gli UNPLAYABLE a raffica.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading

from .. import ARTISTI_REVISIONATI, BASE_DIR, DATA_DIR
from .. import store

log = logging.getLogger(__name__)

QUEUE_FILE = os.path.join(DATA_DIR, "queue.json")

MODES = {
    # modalità → argomenti passati allo script
    "scrape": ["--skip-lyrics"],
    "lyrics": ["--lyrics-only"],
    "full": [],
}


class RunnerError(RuntimeError):
    """Errore di avvio di un run (es. ce n'è già uno in corso)."""


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def reconcile() -> None:
    """
    All'avvio del servizio: un run marcato 'running' il cui processo non esiste
    più è stato interrotto da un riavvio della macchina o da un OOM. Va chiuso,
    altrimenti bloccherebbe per sempre il lock del run singolo.
    """
    active = store.run_active()
    if active and not _pid_alive(active.get("pid")):
        log.warning("Run #%s risulta attivo ma il processo non esiste: lo chiudo.", active["id"])
        store.run_finish(active["id"], "interrupted", None, "Processo non più attivo")


def _write_queue_file(artists: list[dict]) -> None:
    """
    Materializza la coda nel formato storico di `newArtists.json`, che è quello
    che lo scraping sa già leggere.
    """
    os.makedirs(DATA_DIR, exist_ok=True)
    payload = [{"name": a["name"], "youtubeArtistId": a["youtube_artist_id"]} for a in artists]
    with open(QUEUE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def start(
    mode: str = "full",
    only_queued: bool = True,
    artists: list[str] | None = None,
    auto_send: bool = False,
) -> int:
    """
    Avvia un run. Restituisce l'id con cui seguirlo.

    :param mode: 'scrape' (solo canzoni), 'lyrics' (solo testi), 'full' (entrambi)
    :param only_queued: solo gli artisti in coda, senza ripassare quelli già nel DB
    :param artists: restringe a specifici id YouTube
    :param auto_send: invia subito al backend invece di fermarsi alla coda di revisione
    """
    if mode not in MODES:
        raise RunnerError(f"Modalità sconosciuta: {mode}")

    reconcile()
    if store.run_active():
        raise RunnerError("C'è già un run in corso: attendere che finisca o annullarlo.")

    pending = store.queue_pending()
    if only_queued and not pending and not artists:
        raise RunnerError(
            "La coda artisti è vuota: aggiungi almeno un artista, oppure lancia "
            "un run su tutto il catalogo."
        )
    _write_queue_file(pending)

    argv = [sys.executable, "-m", "dbUpdater.main", "--queue-file", QUEUE_FILE]
    argv += MODES[mode]
    if only_queued:
        argv.append("--only-queued")
    for artist_id in artists or []:
        argv += ["--artist", artist_id]
    if not auto_send:
        # Default: i JSON si fermano in ArtistiRevisionati per l'approvazione.
        argv.append("--no-send")

    run_id = store.run_create(mode, argv[1:])
    log_path, events_path = store.run_paths(run_id)

    env = os.environ.copy()
    env["UPDATER_EVENTS_FILE"] = events_path
    env["UPDATER_DATA_DIR"] = DATA_DIR
    env["PYTHONUNBUFFERED"] = "1"

    try:
        log_file = open(log_path, "a", encoding="utf-8", buffering=1)
        proc = subprocess.Popen(
            argv,
            cwd=BASE_DIR,
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        store.run_finish(run_id, "failed", None, f"Avvio fallito: {e}")
        raise RunnerError(f"Impossibile avviare il run: {e}") from e

    store.run_set_pid(run_id, proc.pid)
    log.info("Run #%d avviato (pid %d): %s", run_id, proc.pid, " ".join(argv[1:]))

    # A fine run vanno segnati come elaborati solo gli artisti che il run ha
    # davvero toccato: un run ristretto a un singolo artista non deve svuotare
    # il resto della coda.
    pending_ids = [a["youtube_artist_id"] for a in pending]
    covered = [i for i in pending_ids if i in set(artists)] if artists else pending_ids

    threading.Thread(
        target=_watch, args=(run_id, proc, log_file, covered), daemon=True
    ).start()
    return run_id


def _watch(run_id: int, proc: subprocess.Popen, log_file, queued_ids: list[str]) -> None:
    """Attende la fine del sottoprocesso e chiude la riga di storico."""
    exit_code = proc.wait()
    try:
        log_file.close()
    except Exception:
        pass

    current = store.run_get(run_id)
    if current and current["status"] == "cancelling":
        store.run_finish(run_id, "cancelled", exit_code)
        log.info("Run #%d annullato.", run_id)
        return

    if exit_code == 0:
        # Gli artisti in coda sono stati elaborati: escono dai "da fare" ma
        # restano nello storico della coda.
        store.queue_mark_processed(queued_ids)
        store.run_finish(run_id, "completed", exit_code)
        log.info("Run #%d completato.", run_id)
    else:
        store.run_finish(run_id, "failed", exit_code, _failure_reason(run_id, exit_code))
        log.error("Run #%d fallito (exit %d).", run_id, exit_code)


def _failure_reason(run_id: int, exit_code: int) -> str:
    """
    Motivo del fallimento da mostrare nel pannello. Il codice di uscita da solo
    non dice niente di utile: si pesca l'ultima riga significativa del log, che
    di norma è l'eccezione che ha fatto morire il run.
    """
    fallback = f"Uscita con codice {exit_code}"
    log_path, _ = store.run_paths(run_id)
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-40:]
    except OSError:
        return fallback

    for line in reversed(tail):
        line = line.strip()
        # Le righe di continuazione del traceback non sono il messaggio utile.
        if line and not line.startswith(("File \"", "Traceback", "  ")):
            return f"{line[:300]} (codice {exit_code})"
    return fallback


def cancel(run_id: int) -> bool:
    """Termina un run in corso. Il gruppo di processi viene chiuso, non solo il padre."""
    run = store.run_get(run_id)
    if not run or run["status"] not in ("starting", "running"):
        return False
    pid = run.get("pid")
    if not _pid_alive(pid):
        store.run_finish(run_id, "interrupted", None, "Processo non più attivo")
        return True

    # Solo il cambio di stato: la riga viene chiusa dal watcher quando il
    # processo è effettivamente morto.
    store.run_set_status(run_id, "cancelling")
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except Exception:
        try:
            os.kill(pid, signal.SIGTERM)
        except Exception:
            return False
    return True


# ── Lettura incrementale di log ed eventi ────────────────────────────────────


def _read_from(path: str, cursor: int) -> tuple[str, int]:
    """Legge un file di testo da un offset in byte. Restituisce (testo, nuovo offset)."""
    if not os.path.isfile(path):
        return "", cursor
    size = os.path.getsize(path)
    if cursor >= size:
        return "", size
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        f.seek(cursor)
        chunk = f.read()
        return chunk, f.tell()


def read_log(run_id: int, cursor: int = 0) -> tuple[str, int]:
    log_path, _ = store.run_paths(run_id)
    return _read_from(log_path, cursor)


def read_events(run_id: int, cursor: int = 0) -> tuple[list[dict], int]:
    """
    Eventi strutturati apparsi dopo `cursor`. Una riga a metà scrittura viene
    scartata insieme all'avanzamento del cursore, così alla lettura successiva
    viene ripresa intera.
    """
    _, events_path = store.run_paths(run_id)
    chunk, new_cursor = _read_from(events_path, cursor)
    if not chunk:
        return [], new_cursor

    lines = chunk.split("\n")
    trailing = lines.pop() if not chunk.endswith("\n") else ""
    if trailing:
        # Riga incompleta: la rileggiamo al prossimo giro.
        new_cursor -= len(trailing.encode("utf-8"))

    parsed = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return parsed, new_cursor


def review_files() -> list[dict]:
    """I JSON in attesa di approvazione, con il numero di canzoni che contengono."""
    return _list_json_dir(ARTISTI_REVISIONATI)



def _list_json_dir(directory: str) -> list[dict]:
    """Elenca i JSON di una cartella con artista, numero di canzoni e data."""
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            stat = os.stat(path)
            with open(path, "r", encoding="utf-8") as f:
                songs = json.load(f)
            out.append(
                {
                    "file": name,
                    "artist": name[:-5],
                    "songs": len(songs) if isinstance(songs, list) else 0,
                    "sizeBytes": stat.st_size,
                    "modifiedAt": stat.st_mtime,
                }
            )
        except Exception as e:
            out.append(
                {"file": name, "artist": name[:-5], "songs": 0, "sizeBytes": 0,
                 "modifiedAt": 0, "error": str(e)[:200]}
            )
    return out
