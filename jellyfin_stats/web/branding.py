"""Logo et favicon personnalisables."""

from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    RedirectResponse,
)

from ..auth import CurrentUser
from .helpers import (
    BRANDING_EXTS,
    BRANDING_HEADERS,
    BRANDING_MAX_BYTES,
    STATIC_DIR,
)


def register(app: FastAPI, context) -> None:
    _render_settings = context._render_settings
    branding_dir = context.branding_dir
    require_admin = context.require_admin
    _branding_file = context._branding_file

    # ------------------------------------------------------------------
    # Branding : logo et favicon personnalisables (stockés dans /config)
    # ------------------------------------------------------------------

    @app.get("/branding/logo")
    def branding_logo():
        f = _branding_file("logo")
        if not f:
            raise HTTPException(status_code=404, detail="Pas de logo personnalisé")
        return FileResponse(f, headers=BRANDING_HEADERS)

    @app.get("/branding/favicon")
    @app.get("/favicon.ico")
    def branding_favicon():
        f = _branding_file("favicon") or STATIC_DIR / "img" / "favicon.svg"
        return FileResponse(f, headers=BRANDING_HEADERS)

    def _save_branding(name: str, upload: UploadFile) -> str | None:
        """Enregistre logo/favicon ; retourne un message d'erreur ou None."""
        ext = Path(upload.filename or "").suffix.lower()
        if ext not in BRANDING_EXTS:
            return (
                f"Format non supporté ({ext or 'sans extension'}) — "
                f"acceptés : {', '.join(sorted(BRANDING_EXTS))}"
            )
        data = upload.file.read(BRANDING_MAX_BYTES + 1)
        if not data:
            return None  # champ laissé vide : on ne touche à rien
        if len(data) > BRANDING_MAX_BYTES:
            return "Fichier trop volumineux (max 2 Mo)"
        branding_dir.mkdir(parents=True, exist_ok=True)
        old = _branding_file(name)
        if old:
            old.unlink()
        (branding_dir / f"{name}{ext}").write_bytes(data)
        return None

    @app.post("/settings/branding")
    def upload_branding(
        request: Request,
        user: CurrentUser = Depends(require_admin),
        logo: UploadFile = File(None),
        favicon: UploadFile = File(None),
    ):
        errors = []
        for name, upload in (("logo", logo), ("favicon", favicon)):
            if upload and upload.filename:
                error = _save_branding(name, upload)
                if error:
                    errors.append(f"{name} : {error}")
        if errors:
            return _render_settings(request, user, branding_error=" — ".join(errors))
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.post("/settings/branding/delete")
    def delete_branding(user: CurrentUser = Depends(require_admin), which: str = Form(...)):
        if which in ("logo", "favicon"):
            f = _branding_file(which)
            if f:
                f.unlink()
        return RedirectResponse("/settings", status_code=303)
