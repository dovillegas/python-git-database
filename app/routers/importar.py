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
from app.models import Cohorte, Comision, Equipo
from app.services.importer import importar_archivo
from app.services import importer, mantenimiento_service, pipeline_service

logger = logging.getLogger(__name__)
router = APIRouter(tags=["importación"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

MAX_UPLOAD_BYTES = 10 * 1024 * 1024
SUPPORTED_EXTENSIONS = {".csv", ".xlsx", ".xls"}
MODOS = {"conservar_panel", "archivo_manda"}


def _preparar_informe(db: Session, ruta: Path, resumen: dict) -> dict:
    """Añade contexto de filas y enlaces reales al informe del importador."""
    try:
        datos = importer._leer_archivo(ruta)
        datos.columns = [importer._normalizar_columna(columna) for columna in datos.columns]
        if datos.columns.duplicated().any():
            return resumen
        filas = []
        for numero, (_, fila) in enumerate(datos.iterrows(), start=2):
            valores = {clave: importer._texto(valor) for clave, valor in fila.items()}
            cohorte = valores.get("cohorte", "")
            if re.fullmatch(r"\d+", cohorte):
                valores["cohorte"] = str(int(cohorte))
            filas.append({"fila": numero, **valores})
    except Exception:
        return resumen

    # Los mensajes de errores del motor de base de datos pueden incluir parámetros.
    dnis = {fila.get("dni", "") for fila in filas} - {""}
    for error in resumen.get("filas_con_error", []):
        mensaje = str(error.get("error", ""))
        for dni in sorted(dnis, key=len, reverse=True):
            mensaje = mensaje.replace(dni, importer._dni_parcial(dni))
        error["error"] = mensaje

    equipos = (db.query(Equipo, Cohorte.anio)
               .join(Comision, Equipo.comision_id == Comision.id)
               .join(Cohorte, Comision.cohorte_id == Cohorte.id).all())
    por_clave = {}
    for equipo, anio in equipos:
        por_clave.setdefault((str(anio), equipo.nombre.strip().casefold()), equipo.id)

    urls_por_clave = {
        (str(anio), equipo.nombre.strip().casefold()): importer._normalizar_url(equipo.url_github)
        for equipo, anio in equipos
    }
    urls_antes_de_fila = {}
    filas_con_error = {error.get("fila") for error in resumen.get("filas_con_error", [])}
    for fila in filas:
        clave = (fila.get("cohorte", ""), fila.get("equipo", "").strip().casefold())
        url_archivo = importer._normalizar_url(fila.get("url_github", ""))
        urls_antes_de_fila[fila["fila"]] = urls_por_clave.get(clave, "")
        if fila["fila"] not in filas_con_error:
            if not urls_por_clave.get(clave):
                urls_por_clave[clave] = url_archivo

    def equipo_id(nombre: str | None, fila: dict | None) -> int | None:
        if not nombre or not fila:
            return None
        return por_clave.get((fila.get("cohorte", ""), nombre.strip().casefold()))

    por_numero = {fila["fila"]: fila for fila in filas}
    usadas_url: set[int] = set()
    for conflicto in resumen.get("conflictos", []):
        fila = por_numero.get(conflicto.get("fila"))
        if conflicto.get("tipo") == "url_distinta":
            fila = next((f for f in filas if f["fila"] not in usadas_url
                         and f["fila"] not in filas_con_error
                         and f.get("equipo", "").strip().casefold() == conflicto.get("equipo", "").strip().casefold()
                         and importer._normalizar_url(f.get("url_github", "")) == conflicto.get("archivo")
                         and urls_antes_de_fila[f["fila"]] == importer._normalizar_url(conflicto.get("actual"))), None)
            if fila:
                conflicto["fila"] = fila["fila"]
                usadas_url.add(fila["fila"])
        if conflicto.get("tipo") == "estudiante_en_otro_equipo":
            conflicto["equipo_archivo_id"] = equipo_id(conflicto.get("equipo_archivo"), fila)
            estudiante = importer._buscar_estudiante(db, fila.get("dni", ""), fila.get("email") or None) if fila else None
            conflicto["equipo_actual_id"] = estudiante.equipo_id if estudiante else None
        else:
            conflicto["equipo_id"] = equipo_id(conflicto.get("equipo"), fila)

    usadas_nuevos: set[int] = set()
    for nuevo in resumen.get("estudiantes_nuevos", []):
        fila = next((f for f in filas if f["fila"] not in usadas_nuevos
                     and f["fila"] not in filas_con_error
                     and f.get("equipo", "").strip().casefold() == nuevo.get("equipo", "").strip().casefold()
                     and f.get("apellido_nombre", "") == nuevo.get("nombre")), None)
        if fila:
            nuevo["fila"] = fila["fila"]
            nuevo["equipo_id"] = equipo_id(nuevo.get("equipo"), fila)
            usadas_nuevos.add(fila["fila"])
    return resumen


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
    modo: str = Form(default="conservar_panel"),
    db: Session = Depends(get_db),
):
    if modo not in MODOS:
        await archivo.close()
        return _render_result(request, error="El modo de importación no es válido.")
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
        ruta = uploads_dir / f"{uuid4().hex}_{modo}{suffix}"
        ruta.write_bytes(contenido)
        resumen = importar_archivo(db, ruta, modo=modo, solo_previsualizar=True)
        resumen = _preparar_informe(db, ruta, resumen)
        if not resumen.get("filas_procesadas"):
            ruta.unlink(missing_ok=True)
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

    return _render_result(request, resumen=resumen, previsualizacion=True, archivo_guardado=ruta.name, modo=modo)


@router.post("/importar/confirmar", name="confirmar_importacion")
def confirmar_importacion(
    request: Request,
    archivo_guardado: str = Form(default=""),
    db: Session = Depends(get_db),
):
    coincidencia = re.fullmatch(r"[0-9a-f]{32}_(conservar_panel|archivo_manda)\.(csv|xlsx|xls)", archivo_guardado)
    if not coincidencia:
        return _render_result(request, error="La previsualización no es válida. Volvé a cargar el archivo.")
    uploads = DATA_DIR / "uploads"
    ruta = uploads / archivo_guardado
    try:
        if not ruta.is_file() or ruta.resolve().parent != uploads.resolve():
            return _render_result(request, error="No se encontró el archivo previsualizado. Volvé a cargarlo.")
        modo = coincidencia.group(1)
        resumen = importar_archivo(db, ruta, modo=modo, solo_previsualizar=False)
        resumen = _preparar_informe(db, ruta, resumen)
        if not any(item.get("fila") is None for item in resumen.get("filas_con_error", [])):
            try:
                ruta.unlink(missing_ok=True)
            except OSError:
                logger.warning("No se pudo borrar el archivo de importación", exc_info=True)
    except Exception:
        db.rollback()
        logger.exception("Error inesperado al confirmar la importación")
        return _render_result(request, error="No se pudo confirmar la importación. Intentá nuevamente.")
    return _render_result(request, resumen=resumen, modo=modo)


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