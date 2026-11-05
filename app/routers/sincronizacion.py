from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Cohorte, Comision, Equipo
from app.services.pipeline_service import (
    ejecutar,
    ejecutar_equipo,
    estado,
    estado_equipo,
    estado_equipos,
)

router = APIRouter(tags=["sincronizacion"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_ACCIONES = {"descargar", "calcular", "sincronizar_calcular"}


def _parse_filter(value: str | None, label: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"El filtro {label} debe ser numérico") from exc


def _validate_action(accion: str) -> str:
    if accion not in _ACCIONES:
        raise HTTPException(status_code=422, detail="La acción debe ser descargar, calcular o sincronizar_calcular")
    return accion


def _page_context(
    db: Session,
    request: Request,
    cohorte_id: int | None,
    comision_id: int | None,
) -> dict[str, Any]:
    cohortes = db.query(Cohorte).order_by(Cohorte.anio.desc()).all()
    consulta_comisiones = db.query(Comision).options(joinedload(Comision.cohorte)).join(Cohorte)
    if cohorte_id is not None:
        consulta_comisiones = consulta_comisiones.filter(Comision.cohorte_id == cohorte_id)
    comisiones = consulta_comisiones.order_by(Cohorte.anio.desc(), Comision.numero).all()
    # Un filtro de comisión incompatible con la cohorte se ignora en vez de
    # devolver una tabla aparentemente vacía por una selección obsoleta.
    if comision_id is not None and not any(item.id == comision_id for item in comisiones):
        comision_id = None
    estados = estado_equipos(db, cohorte_id, comision_id)
    equipos = [item["equipo"] for item in estados]
    estados_por_equipo = {item["equipo"].id: item["estado"] for item in estados}
    return {
        "request": request,
        "vista": "pagina",
        "equipos": equipos,
        "estado_equipos": estados_por_equipo,
        "cohortes": cohortes,
        "comisiones": comisiones,
        "cohorte_seleccionada": cohorte_id,
        "comision_seleccionada": comision_id,
        "progreso": estado(),
    }


def _render_progress(request: Request, progreso: dict[str, Any], db: Session):
    cohorte_id = progreso.get("cohorte")
    comision_id = progreso.get("comision")
    page_context = _page_context(db, request, cohorte_id, comision_id)
    return templates.TemplateResponse(
        request=request,
        name="sincronizacion.html",
        context={**page_context, "vista": "progreso", "progreso": progreso},
    )


@router.get("/sincronizacion", name="pagina_sincronizacion")
def pagina_sincronizacion(
    request: Request,
    cohorte: str | None = None,
    comision: str | None = None,
    db: Session = Depends(get_db),
):
    contexto = _page_context(
        db,
        request,
        _parse_filter(cohorte, "cohorte"),
        _parse_filter(comision, "comisión"),
    )
    return templates.TemplateResponse(request=request, name="sincronizacion.html", context=contexto)


@router.post("/sincronizacion/ejecutar", name="ejecutar_sincronizacion")
def ejecutar_sincronizacion(
    request: Request,
    accion: str,
    cohorte: str | None = None,
    comision: str | None = None,
    db: Session = Depends(get_db),
):
    accion = _validate_action(accion)
    cohorte_id = _parse_filter(cohorte, "cohorte")
    comision_id = _parse_filter(comision, "comisión")
    ejecutar(accion, cohorte_id, comision_id)
    progreso = estado()
    if progreso["running"] and progreso.get("accion") != accion:
        progreso["mensaje"] = "Ya hay otra sincronización en curso."
    elif not progreso["running"] and not progreso["total"]:
        progreso["mensaje"] = "No hay equipos que coincidan con los filtros."
    else:
        progreso["mensaje"] = "Proceso iniciado. El panel se actualizará automáticamente."
    return _render_progress(request, progreso, db)


@router.get("/sincronizacion/progreso", name="progreso_sincronizacion")
def progreso_sincronizacion(request: Request, db: Session = Depends(get_db)):
    return _render_progress(request, estado(), db)


@router.post("/sincronizacion/equipo/{equipo_id}", name="ejecutar_accion_equipo")
def ejecutar_accion_equipo(
    equipo_id: int,
    request: Request,
    accion: str,
    db: Session = Depends(get_db),
):
    accion = _validate_action(accion)
    equipo = (
        db.query(Equipo)
        .options(joinedload(Equipo.comision).joinedload(Comision.cohorte))
        .filter(Equipo.id == equipo_id)
        .first()
    )
    if equipo is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    resultado = ejecutar_equipo(db, equipo, accion)
    estado_actual = estado_equipo(db, equipo)
    return templates.TemplateResponse(
        request=request,
        name="sincronizacion.html",
        context={
            "vista": "fila",
            "equipo": equipo,
            "estado": estado_actual,
            "mensaje": resultado.get("error") or resultado.get("mensaje"),
            "error": bool(resultado.get("error")),
        },
    )