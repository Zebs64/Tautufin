"""Import d'historique (Playback Reporting, Streamystats) et inférence."""

import logging
import re
import secrets
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile

from .. import (
    import_playback,
    import_streamystats,
    infer_history,
)
from ..auth import CurrentUser
from .helpers import (
    IMPORT_UPLOAD_MAX_BYTES,
)

logger = logging.getLogger(__name__)


def register(app: FastAPI, context) -> None:
    config = context.config
    api = context.api
    config_dir = context.config_dir
    require_admin = context.require_admin

    @app.post("/api/import/upload")
    def api_import_upload(user: CurrentUser = Depends(require_admin), file: UploadFile = File(...)):
        """Réception d'un backup .tsv ou d'un playback_reporting.db envoyé
        depuis le navigateur. Stocké dans <config>/uploads/ (un seul fichier
        conservé), puis analysé/importé via son chemin comme un fichier local."""
        safe_name = (
            re.sub(r"[^A-Za-z0-9._-]", "_", Path(file.filename or "").name).lstrip(".")
            or "backup-upload"
        )
        uploads_dir = config_dir / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        # Écriture dans un .tmp : l'upload précédent n'est remplacé qu'une
        # fois le nouveau reçu et validé.
        tmp = uploads_dir / f".upload-{secrets.token_hex(6)}.tmp"
        size = 0
        try:
            with open(tmp, "wb") as out:
                while chunk := file.file.read(1024 * 1024):
                    size += len(chunk)
                    if size > IMPORT_UPLOAD_MAX_BYTES:
                        raise HTTPException(
                            status_code=413, detail="Fichier trop volumineux (max 512 Mo)"
                        )
                    out.write(chunk)
            if size == 0:
                raise HTTPException(status_code=400, detail="Fichier vide")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        for old in uploads_dir.iterdir():  # on ne garde que le dernier upload
            if old.is_file() and old != tmp:
                old.unlink()
        dest = uploads_dir / safe_name
        tmp.rename(dest)
        logger.info("Backup uploadé par %s : %s (%d octets)", user.username, safe_name, size)
        return {"path": str(dest), "filename": safe_name, "size": size}

    @app.post("/api/import/analyze")
    def api_import_analyze(payload: dict, user: CurrentUser = Depends(require_admin)):
        # Détection automatique : backup JSON Streamystats vs base/backup
        # Playback Reporting (.db/.tsv).
        path = (payload.get("path") or "").strip()
        try:
            if import_streamystats.looks_like_streamystats(path):
                return import_streamystats.analyze(path)
            return import_playback.analyze(path)
        except import_playback.ImportError_ as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/import/run")
    def api_import_run(payload: dict, user: CurrentUser = Depends(require_admin)):
        path = (payload.get("path") or "").strip()
        module = (
            import_streamystats
            if import_streamystats.looks_like_streamystats(path)
            else import_playback
        )
        try:
            return module.run_import(path, api, config.minimum_duration, config.minimum_percent)
        except import_playback.ImportError_ as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    # ------------------------------------------------------------------
    # Inférence d'historique depuis le statut « Lu » de Jellyfin (admin)
    # ------------------------------------------------------------------

    @app.post("/api/infer/run")
    def api_infer_run(payload: dict, user: CurrentUser = Depends(require_admin)):
        if not config.jellyfin_configured:
            raise HTTPException(status_code=400, detail="Jellyfin non configuré")
        target = (payload.get("user_id") or "").strip() or None
        report = infer_history.infer_history(
            api, config.minimum_duration, config.minimum_percent, user_id=target
        )
        report["inferred_count"] = infer_history.count_inferred()
        return report

    @app.post("/api/infer/cleanup")
    def api_infer_cleanup(payload: dict, user: CurrentUser = Depends(require_admin)):
        target = (payload.get("user_id") or "").strip() or None
        deleted = infer_history.delete_inferred(user_id=target)
        return {"deleted": deleted, "inferred_count": infer_history.count_inferred()}

    # ------------------------------------------------------------------
    # Webhook Jellyfin (plugin Webhook, événements temps réel)
