"""Visionnages : regroupement des segments d'une même lecture.

Un film repris après une pause, ou fini le lendemain, produit plusieurs lignes
dans ``session_history``. Elles forment un seul *visionnage* : les statistiques
comptent les visionnages (une lecture, datée de son premier segment) et
l'historique les affiche sur une ligne dépliable. Les lignes brutes ne
changent pas : ``viewing_id`` (identifiant du premier segment) est une donnée
dérivée, recalculée quand la règle change (``RULE_VERSION``).

Règle (même utilisateur, même média) : un segment prolonge le visionnage en
cours s'il commence au plus 24 h après la fin du précédent et si le visionnage
n'était pas terminé (progression < 90 %). La progression d'une ligne live est
la position atteinte dans le média ; celle des autres sources (imports), la
part cumulée vue (somme des durées / durée du média), car leur pourcentage est
une proportion et non une position. Jamais de regroupement pour une lecture
estimée (inférée, ou reconstituée par Streamystats), ni quand la progression
est inconnue.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from itertools import groupby

RULE_VERSION = "1"
MAX_GAP = timedelta(hours=24)
FINISHED_PERCENT = 90.0
_RULE_KEY = "viewings_rule"

_COLUMNS = (
    "id, jellyfin_user_id, item_id, started_at, stopped_at, play_duration,"
    " runtime_seconds, percent_complete, source"
)


@dataclass
class Segment:
    id: int
    jellyfin_user_id: str
    item_id: str | None
    started_at: str
    stopped_at: str | None
    play_duration: int | None
    runtime_seconds: int | None
    percent_complete: float | None
    source: str | None


def is_estimated(seg: Segment) -> bool:
    """Lecture reconstituée (date = dernière lecture connue, 100 % supposé) :
    inférée par Tautufin, ou importée de Streamystats avec début = fin."""
    return seg.source == "infer" or (
        seg.source == "import" and bool(seg.stopped_at) and seg.stopped_at == seg.started_at
    )


def _parse(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)[:19])
    except (TypeError, ValueError):
        return None


def _end(seg: Segment, start: datetime) -> datetime:
    stop = _parse(seg.stopped_at)
    if stop and stop > start:
        return stop
    return start + timedelta(seconds=seg.play_duration or 0)


def assign(segments: list[Segment]) -> dict[int, int]:
    """Segments d'un même (utilisateur, média) -> {id: viewing_id}."""
    result: dict[int, int] = {}
    ordered = sorted(segments, key=lambda s: (s.started_at or "", s.id))
    viewing_id = None
    watched = 0
    progress: float | None = None
    previous: Segment | None = None
    previous_end: datetime | None = None
    for seg in ordered:
        start = _parse(seg.started_at)
        continues = (
            previous is not None
            and seg.item_id is not None
            and start is not None
            and previous_end is not None
            and not is_estimated(seg)
            and not is_estimated(previous)
            and progress is not None
            and progress < FINISHED_PERCENT
            and start - previous_end <= MAX_GAP
        )
        if continues:
            watched += seg.play_duration or 0
        else:
            viewing_id = seg.id
            watched = seg.play_duration or 0
        result[seg.id] = viewing_id
        if seg.source == "live" and seg.percent_complete is not None:
            progress = seg.percent_complete
        elif seg.runtime_seconds:
            progress = watched * 100.0 / seg.runtime_seconds
        else:
            progress = seg.percent_complete
        previous, previous_end = seg, (_end(seg, start) if start else None)
    return result


def _apply(conn, rows) -> int:
    """Calcule et enregistre les viewing_id de lignes triées par partition."""
    updates = []
    partition = lambda r: (r[1], r[2])  # noqa: E731
    for _, group in groupby(rows, key=partition):
        updates += [(v, k) for k, v in assign([Segment(*r) for r in group]).items()]
    conn.executemany("UPDATE session_history SET viewing_id = ? WHERE id = ?", updates)
    return len(updates)


def refresh(conn) -> int:
    """Recalcule les partitions (utilisateur, média) qui contiennent une ligne
    sans viewing_id : à appeler après toute insertion. Renvoie le nombre de
    lignes recalculées."""
    # Média inconnu : chaque ligne est son propre visionnage.
    done = conn.execute(
        "UPDATE session_history SET viewing_id = id WHERE viewing_id IS NULL AND item_id IS NULL"
    ).rowcount
    rows = conn.execute(
        f"""
        SELECT {_COLUMNS} FROM session_history
        WHERE (jellyfin_user_id, item_id) IN (
            SELECT jellyfin_user_id, item_id FROM session_history
            WHERE viewing_id IS NULL AND item_id IS NOT NULL
        )
        ORDER BY jellyfin_user_id, item_id, started_at, id
        """
    ).fetchall()
    return done + _apply(conn, rows)


def invalidate(conn, where_sql: str, params=()) -> None:
    """Avant une suppression : marque à recalculer les partitions des lignes
    visées (un segment supprimé peut être le premier d'un visionnage)."""
    conn.execute(
        f"""
        UPDATE session_history SET viewing_id = NULL
        WHERE (jellyfin_user_id, item_id) IN (
            SELECT jellyfin_user_id, item_id FROM session_history
            WHERE item_id IS NOT NULL AND ({where_sql})
        )
        """,
        params,
    )


def ensure(conn) -> None:
    """Au démarrage : recalcul complet si la règle a changé, sinon rattrapage
    des lignes sans viewing_id (ex. écrites par une version plus ancienne)."""
    row = conn.execute("SELECT value FROM sync_state WHERE key = ?", (_RULE_KEY,)).fetchone()
    if row is None or row[0] != RULE_VERSION:
        conn.execute("UPDATE session_history SET viewing_id = NULL")
        conn.execute(
            "INSERT INTO sync_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_RULE_KEY, RULE_VERSION),
        )
    refresh(conn)
