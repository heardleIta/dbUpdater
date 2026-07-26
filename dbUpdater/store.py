"""
Stato persistente del servizio di controllo: coda degli artisti da elaborare e
storico dei run. SQLite su volume, così sopravvive alla ricreazione del
container.
"""

import json
import os
import sqlite3
from datetime import datetime, timezone

from . import DATA_DIR

DB_PATH = os.path.join(DATA_DIR, "updater.db")
RUNS_DIR = os.path.join(DATA_DIR, "runs")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    with _connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS artist_queue (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                name              TEXT NOT NULL,
                youtube_artist_id TEXT NOT NULL UNIQUE,
                added_at          TEXT NOT NULL,
                processed_at      TEXT
            );

            CREATE TABLE IF NOT EXISTS runs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                mode        TEXT NOT NULL,
                status      TEXT NOT NULL,
                pid         INTEGER,
                args        TEXT,
                started_at  TEXT NOT NULL,
                finished_at TEXT,
                exit_code   INTEGER,
                error       TEXT
            );

            CREATE TABLE IF NOT EXISTS sent_artists (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                artist   TEXT NOT NULL,
                file     TEXT NOT NULL,
                songs    INTEGER NOT NULL,
                duration REAL,
                sent_at  TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_runs_started ON runs (started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_sent_at ON sent_artists (sent_at DESC);
            """
        )


# ── Artisti inviati ──────────────────────────────────────────────────────────


def sent_add(artist: str, file: str, songs: int, duration: float | None = None) -> None:
    """
    Registra un invio riuscito. È il rimpiazzo dell'archivio di JSON: teniamo il
    resoconto (chi, quando, quante canzoni) e non il contenuto, che pesava
    centinaia di KB per artista.
    """
    with _connect() as conn:
        conn.execute(
            "INSERT INTO sent_artists (artist, file, songs, duration, sent_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (artist, file, songs, duration, _now()),
        )


def sent_list(limit: int = 20) -> list[dict]:
    """Gli invii riusciti, dal più recente."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sent_artists ORDER BY sent_at DESC, id DESC LIMIT ?",
                (limit,),
            )
        ]


def sent_find(file: str) -> dict | None:
    """L'invio più recente di un dato file, se c'è. Serve a rendere idempotente l'approvazione."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM sent_artists WHERE file = ? ORDER BY id DESC LIMIT 1", (file,)
        ).fetchone()
        return dict(row) if row else None


def sent_count() -> int:
    with _connect() as conn:
        return conn.execute("SELECT count(*) FROM sent_artists").fetchone()[0]


def sent_import_archive(directory: str) -> int:
    """
    Porta nel recap i JSON archiviati dal vecchio meccanismo, che spostava i file
    in ArtistiSongSent invece di registrarne il resoconto. Senza questo passaggio
    l'elenco degli artisti già inseriti partirebbe vuoto, perdendo la storia.

    I file non vengono toccati: la cancellazione resta una decisione manuale.
    Idempotente, riconosce quelli già importati dal nome file.
    """
    if not os.path.isdir(directory):
        return 0

    imported = 0
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json") or sent_find(name):
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as f:
                songs = json.load(f)
            count = len(songs) if isinstance(songs, list) else 0
            sent_at = datetime.fromtimestamp(
                os.path.getmtime(path), tz=timezone.utc
            ).isoformat()
        except Exception:
            continue

        with _connect() as conn:
            # `duration` resta NULL: di questi invii storici non conosciamo il tempo.
            conn.execute(
                "INSERT INTO sent_artists (artist, file, songs, duration, sent_at) "
                "VALUES (?, ?, ?, NULL, ?)",
                (name[:-5], name, count, sent_at),
            )
        imported += 1
    return imported


# ── Coda artisti ─────────────────────────────────────────────────────────────


def queue_list(include_processed: bool = True) -> list[dict]:
    sql = "SELECT * FROM artist_queue"
    if not include_processed:
        sql += " WHERE processed_at IS NULL"
    sql += " ORDER BY processed_at IS NOT NULL, id DESC"
    with _connect() as conn:
        return [dict(r) for r in conn.execute(sql)]


def queue_pending() -> list[dict]:
    """Gli artisti che un run deve ancora elaborare, nell'ordine di inserimento."""
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM artist_queue WHERE processed_at IS NULL ORDER BY id ASC"
            )
        ]


def queue_add(name: str, youtube_artist_id: str) -> str:
    """
    Aggiunge un artista alla coda. Restituisce 'added' oppure 'duplicate' se
    quell'id YouTube è già presente (la UI mostra il riepilogo prima di confermare).
    """
    with _connect() as conn:
        existing = conn.execute(
            "SELECT id FROM artist_queue WHERE youtube_artist_id = ?", (youtube_artist_id,)
        ).fetchone()
        if existing:
            return "duplicate"
        conn.execute(
            "INSERT INTO artist_queue (name, youtube_artist_id, added_at) VALUES (?, ?, ?)",
            (name, youtube_artist_id, _now()),
        )
        return "added"


def queue_remove(queue_id: int) -> bool:
    with _connect() as conn:
        cur = conn.execute("DELETE FROM artist_queue WHERE id = ?", (queue_id,))
        return cur.rowcount > 0


def queue_remove_processed() -> int:
    """
    Svuota le voci già elaborate. La coda accumulava per sempre lo storico e si
    poteva ripulire solo una riga per volta. Restituisce quante ne ha rimosse.
    """
    with _connect() as conn:
        cur = conn.execute("DELETE FROM artist_queue WHERE processed_at IS NOT NULL")
        return cur.rowcount


def queue_mark_processed(youtube_artist_ids: list[str]) -> None:
    if not youtube_artist_ids:
        return
    placeholders = ",".join("?" for _ in youtube_artist_ids)
    with _connect() as conn:
        conn.execute(
            f"UPDATE artist_queue SET processed_at = ? "
            f"WHERE youtube_artist_id IN ({placeholders}) AND processed_at IS NULL",
            [_now(), *youtube_artist_ids],
        )


# ── Run ──────────────────────────────────────────────────────────────────────


def run_create(mode: str, args: list[str]) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO runs (mode, status, args, started_at) VALUES (?, 'starting', ?, ?)",
            (mode, json.dumps(args), _now()),
        )
        return cur.lastrowid


def run_set_pid(run_id: int, pid: int) -> None:
    with _connect() as conn:
        conn.execute("UPDATE runs SET pid = ?, status = 'running' WHERE id = ?", (pid, run_id))


def run_set_status(run_id: int, status: str) -> None:
    """Cambia stato senza chiudere il run (usato per 'cancelling')."""
    with _connect() as conn:
        conn.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


def run_finish(run_id: int, status: str, exit_code: int | None, error: str | None = None) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, exit_code = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, exit_code, error, _now(), run_id),
        )


def run_get(run_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def run_list(limit: int = 20) -> list[dict]:
    with _connect() as conn:
        return [
            dict(r)
            for r in conn.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))
        ]


def run_active() -> dict | None:
    """
    Il run attualmente in corso, se c'è (ne ammettiamo uno solo alla volta).
    Include 'cancelling': finché il processo non è davvero morto non se ne può
    avviare un altro, altrimenti due scraping si contenderebbero il circuito Tor.
    """
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status IN ('starting', 'running', 'cancelling') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(row) if row else None


def run_paths(run_id: int) -> tuple[str, str]:
    """(file di log, file eventi JSONL) del run."""
    os.makedirs(RUNS_DIR, exist_ok=True)
    return (
        os.path.join(RUNS_DIR, f"{run_id}.log"),
        os.path.join(RUNS_DIR, f"{run_id}.jsonl"),
    )
