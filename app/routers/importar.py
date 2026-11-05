from __future__ import annotations

import logging
from pathlib import Path
import re
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import DATA_DIR, get_db
from app.services.importer import importar_archivo
from app.services import mantenimiento_service, pipeline_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["importación"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}


def _render_import_page(request: Request, **context: object):
    mantenimiento = _mantenimiento_context()
    return templates.TemplateResponse(
        request=request,
        name="importar.html",
        context={"request": request, **mantenimiento, **context},
    )


def _mantenimiento_context(cohorte_seleccionada: int | None = None) -> dict[str, object]:
    resumen = mantenimiento_service.resumen()
    try:
        pipeline = pipeline_service.estado()
        pipeline_activo = bool(pipeline.get("running") or pipeline.get("activo"))
        error_pipeline = None
    except Exception as exc:
        pipeline_activo = True
        error_pipeline = f"No se pudo verificar el lote activo: {exc}"

    bloqueado = pipeline_activo or not resumen.get("ok")
    if pipeline_activo:
        aviso = "Hay un lote activo o no se pudo verificar su estado. Las acciones de mantenimiento están deshabilitadas."
    elif not resumen.get("ok"):
        aviso = resumen.get("mensaje", "El resumen no está disponible; las acciones están deshabilitadas.")
    else:
        aviso = error_pipeline

    return {
        "mantenimiento_resumen": resumen,
        "mantenimiento_bloqueado": bloqueado,
        "mantenimiento_aviso": aviso,
        "mantenimiento_cohortes": resumen.get("por_cohorte", []),
        "cohorte_seleccionada": cohorte_seleccionada,
    }


def _render_mantenimiento(request: Request, resultado: dict[str, object], cohorte_anio: int | None = None):
    contexto = {
        "request": request,
        "resultado_mantenimiento": resultado,
        **_mantenimiento_context(cohorte_anio),
    }
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            request=request,
            name="mantenimiento_panel.html",
            context=contexto,
        )
    return _render_import_page(request, **contexto)


def _render_result(request: Request, **context: object):
    if request.headers.get("HX-Request", "").lower() == "true":
        return templates.TemplateResponse(
            request=request,
            name="resultado_importacion.html",
            context=context,
        )
    return _render_import_page(request, **context)


@router.get("/importar", name="pagina_importar")
def pagina_importar(request: Request):
    return _render_import_page(request)


@router.post("/importar", name="importar_archivo")
async def importar(
    request: Request,
    archivo: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    filename = archivo.filename or ""
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        await archivo.close()
        return _render_result(
            request,
            error="Formato no admitido. Subí un archivo CSV, XLSX o XLS.",
        )

    contenido = await archivo.read(MAX_UPLOAD_BYTES + 1)
    await archivo.close()
    if len(contenido) > MAX_UPLOAD_BYTES:
        return _render_result(
            request,
            error="El archivo supera el límite de 10 MB.",
        )
    if not contenido:
        return _render_result(request, error="El archivo está vacío.")

    try:
        uploads_dir = DATA_DIR / "uploads"
        uploads_dir.mkdir(parents=True, exist_ok=True)
        ruta = uploads_dir / f"{uuid4().hex}{suffix}"
        ruta.write_bytes(contenido)
        resumen = importar_archivo(db, ruta)
    except ValueError as exc:
        db.rollback()
        return _render_result(request, error=str(exc))
    except Exception:
        db.rollback()
        logger.exception("Error inesperado durante la importación de %s", filename)
        return _render_result(
            request,
            error="Ocurrió un error al procesar el archivo. Revisá su formato e intentá nuevamente.",
        )

    return _render_result(request, resumen=resumen)


def _ejecutar_mantenimiento(
    request: Request,
    db: Session,
    accion: str,
    confirmacion: str,
    cohorte_anio: int | None = None,
):
    if confirmacion != "BORRAR":
        resultado: dict[str, object] = {
            "ok": False,
            "mensaje": 'Confirmación inválida. Escribí exactamente "BORRAR" para continuar.',
        }
    else:
        try:
            if accion == "metricas":
                resultado = mantenimiento_service.borrar_metricas(db)
            elif accion == "datos":
                resultado = mantenimiento_service.borrar_datos(db)
            elif accion == "repos":
                resultado = mantenimiento_service.borrar_repos(cohorte_anio)
            else:
                resultado = mantenimiento_service.borrar_todo(db)
        except Exception as exc:
            db.rollback()
            logger.exception("Error inesperado durante mantenimiento (%s)", accion)
            resultado = {"ok": False, "mensaje": f"No se pudo completar el mantenimiento: {exc}"}

    ruta_respaldo = resultado.get("respaldo")
    if ruta_respaldo:
        resultado["respaldo_nombre"] = Path(str(ruta_respaldo)).name
    return _render_mantenimiento(request, resultado, cohorte_anio)


@router.post("/mantenimiento/metricas", name="mantenimiento_metricas")
def mantenimiento_metricas(
    request: Request,
    confirmacion: str = Form(default=""),
    db: Session = Depends(get_db),
):
    return _ejecutar_mantenimiento(request, db, "metricas", confirmacion)


@router.post("/mantenimiento/datos", name="mantenimiento_datos")
def mantenimiento_datos(
    request: Request,
    confirmacion: str = Form(default=""),
    db: Session = Depends(get_db),
):
    return _ejecutar_mantenimiento(request, db, "datos", confirmacion)


@router.post("/mantenimiento/repos", name="mantenimiento_repos")
def mantenimiento_repos(
    request: Request,
    confirmacion: str = Form(default=""),
    cohorte_anio: str = Form(default=""),
    db: Session = Depends(get_db),
):
    cohorte = None
    if cohorte_anio:
        try:
            cohorte = int(cohorte_anio)
        except ValueError:
            return _render_mantenimiento(
                request,
                {"ok": False, "mensaje": "La cohorte seleccionada no es válida."},
            )
        if not any(item["anio"] == cohorte for item in mantenimiento_service.resumen().get("por_cohorte", [])):
            return _render_mantenimiento(
                request,
                {"ok": False, "mensaje": "La cohorte seleccionada no existe."},
                cohorte,
            )
    return _ejecutar_mantenimiento(request, db, "repos", confirmacion, cohorte)


@router.post("/mantenimiento/todo", name="mantenimiento_todo")
def mantenimiento_todo(
    request: Request,
    confirmacion: str = Form(default=""),
    db: Session = Depends(get_db),
):
    return _ejecutar_mantenimiento(request, db, "todo", confirmacion)


@router.get("/mantenimiento/respaldo", name="descargar_respaldo")
def descargar_respaldo(request: Request, archivo: str | None = None):
    if archivo is None:
        resultado = mantenimiento_service.respaldar_base()
        if not resultado.get("ok"):
            return _render_import_page(request, resultado_mantenimiento=resultado)
        ruta = Path(str(resultado["ruta"]))
    else:
        if not re.fullmatch(r"app_\d{8}_\d{6}\.db", archivo):
            return _render_import_page(
                request,
                resultado_mantenimiento={"ok": False, "mensaje": "El nombre del respaldo no es válido."},
            )
        ruta = DATA_DIR / "backups" / archivo
        try:
            if not ruta.is_file() or ruta.resolve().parent != (DATA_DIR / "backups").resolve():
                return _render_import_page(
                    request,
                    resultado_mantenimiento={"ok": False, "mensaje": "No se encontró el respaldo solicitado."},
                )
        except OSError as exc:
            return _render_import_page(
                request,
                resultado_mantenimiento={"ok": False, "mensaje": f"No se pudo acceder al respaldo: {exc}"},
            )
    return FileResponse(ruta, filename=ruta.name, media_type="application/octet-stream")