from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from urllib.parse import urlencode

from app.database import SessionLocal, get_db
from app.models import AutorGit, Cohorte, Comision, Equipo, Estudiante, MetricaProyecto
from app.services.metricas_service import calcular_equipo
from app.services.pipeline_service import estado
from app.services.equipos_service import (
    editar_integrante as servicio_editar_integrante,
    quitar_integrante as servicio_quitar_integrante,
)

router = APIRouter(tags=["autores Git"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
_ESTADOS = {"pendientes", "asignados", "ignorados"}


def _filtros(db: Session, cohorte_id: int | None, comision_id: int | None, equipo_id: int | None) -> dict[str, Any]:
    cohortes = db.query(Cohorte).order_by(Cohorte.anio.desc()).all()
    comisiones_query = db.query(Comision).options(joinedload(Comision.cohorte)).join(Cohorte)
    if cohorte_id is not None:
        comisiones_query = comisiones_query.filter(Comision.cohorte_id == cohorte_id)
    comisiones = comisiones_query.order_by(Cohorte.anio.desc(), Comision.numero).all()
    if comision_id is not None and not any(item.id == comision_id for item in comisiones):
        comision_id = None

    equipos_query = db.query(Equipo).join(Comision).join(Cohorte)
    if cohorte_id is not None:
        equipos_query = equipos_query.filter(Cohorte.id == cohorte_id)
    if comision_id is not None:
        equipos_query = equipos_query.filter(Comision.id == comision_id)
    equipos = equipos_query.order_by(Cohorte.anio.desc(), Comision.numero, Equipo.nombre).all()
    if equipo_id is not None and not any(item.id == equipo_id for item in equipos):
        equipo_id = None
    return {
        "cohortes": cohortes,
        "comisiones": comisiones,
        "equipos": equipos,
        "cohorte_seleccionada": cohorte_id,
        "comision_seleccionada": comision_id,
        "equipo_seleccionado": equipo_id,
    }


def _consulta_autores(
    db: Session,
    pestana: str,
    cohorte_id: int | None,
    comision_id: int | None,
    equipo_id: int | None,
):
    consulta = (
        db.query(AutorGit)
        .join(Equipo, AutorGit.equipo_id == Equipo.id)
        .join(Comision, Equipo.comision_id == Comision.id)
        .join(Cohorte, Comision.cohorte_id == Cohorte.id)
        .options(
            joinedload(AutorGit.equipo).joinedload(Equipo.comision).joinedload(Comision.cohorte),
            joinedload(AutorGit.equipo).joinedload(Equipo.estudiantes),
        )
    )
    if pestana == "pendientes":
        consulta = consulta.filter(AutorGit.estudiante_id.is_(None), AutorGit.ignorado.is_(False))
    elif pestana == "asignados":
        consulta = consulta.filter(AutorGit.estudiante_id.is_not(None), AutorGit.ignorado.is_(False))
    else:
        consulta = consulta.filter(AutorGit.ignorado.is_(True))
    if cohorte_id is not None:
        consulta = consulta.filter(Cohorte.id == cohorte_id)
    if comision_id is not None:
        consulta = consulta.filter(Comision.id == comision_id)
    if equipo_id is not None:
        consulta = consulta.filter(Equipo.id == equipo_id)
    return consulta.order_by(AutorGit.commits.desc(), AutorGit.nombre_git, AutorGit.email)


def _ids_calculados(db: Session, autores: list[AutorGit]) -> set[int]:
    equipo_ids = {autor.equipo_id for autor in autores}
    if not equipo_ids:
        return set()
    return {
        equipo_id
        for (equipo_id,) in db.query(MetricaProyecto.equipo_id)
        .filter(MetricaProyecto.equipo_id.in_(equipo_ids))
        .distinct()
        .all()
    }


def _pendientes_count(
    db: Session,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    equipo_id: int | None = None,
) -> int:
    consulta = (
        db.query(func.count(AutorGit.id))
        .join(Equipo, AutorGit.equipo_id == Equipo.id)
        .join(Comision, Equipo.comision_id == Comision.id)
        .filter(AutorGit.estudiante_id.is_(None), AutorGit.ignorado.is_(False))
    )
    if cohorte_id is not None:
        consulta = consulta.filter(Comision.cohorte_id == cohorte_id)
    if comision_id is not None:
        consulta = consulta.filter(Comision.id == comision_id)
    if equipo_id is not None:
        consulta = consulta.filter(Equipo.id == equipo_id)
    return int(consulta.scalar() or 0)


def _render_fila(
    request: Request,
    db: Session,
    autor: AutorGit,
    pestana: str,
    filtros: dict[str, int | None],
    aviso: str | None = None,
    aviso_tipo: str = "success",
    resumen: str | None = None,
):
    equipo_id = autor.equipo_id
    equipo_calculado = bool(
        db.query(MetricaProyecto.id)
        .filter(MetricaProyecto.equipo_id == equipo_id)
        .first()
    )
    return templates.TemplateResponse(
        request=request,
        name="_autores_tabla.html",
        context={
            "request": request,
            "fila_respuesta": autor,
            "equipo_id": equipo_id,
            "pestana": pestana,
            "equipo_calculado": equipo_calculado,
            "filtros": filtros,
            "aviso": aviso,
            "aviso_tipo": aviso_tipo,
            "resumen": resumen,
            "pendientes_count": _pendientes_count(db),
        },
    )


@router.get("/autores", name="lista_autores")
def lista_autores(
    request: Request,
    estado: str = "pendientes",
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    equipo_id: int | None = None,
    db: Session = Depends(get_db),
):
    if estado not in _ESTADOS:
        raise HTTPException(status_code=422, detail="La pestaña debe ser pendientes, asignados o ignorados")
    for value, label in ((cohorte_id, "cohorte"), (comision_id, "comisión"), (equipo_id, "equipo")):
        if value is not None and value < 1:
            raise HTTPException(status_code=422, detail=f"El filtro {label} debe ser positivo")

    filtros = _filtros(db, cohorte_id, comision_id, equipo_id)
    cohorte_id = filtros["cohorte_seleccionada"]
    comision_id = filtros["comision_seleccionada"]
    equipo_id = filtros["equipo_seleccionado"]
    autores = _consulta_autores(db, estado, cohorte_id, comision_id, equipo_id).all()
    contexto = {
        "request": request,
        "estado": estado,
        "listado": autores,
        "equipo_id": equipo_id,
        "pestana": estado,
        "equipos_calculados": _ids_calculados(db, autores),
        "pendientes_count": _pendientes_count(db),
        "filtros": {
            "cohorte_id": cohorte_id,
            "comision_id": comision_id,
            "equipo_id": equipo_id,
        },
        **filtros,
    }
    return templates.TemplateResponse(request=request, name="autores.html", context=contexto)


def _actualizar_autor(
    autor_id: int,
    request: Request,
    accion: str,
    estudiante_id: int | None,
    estado_actual: str,
    cohorte_id: int | None,
    comision_id: int | None,
    equipo_id: int | None,
    db_request: Session,
):
    if estado_actual not in _ESTADOS:
        estado_actual = "pendientes"
    filtros: dict[str, int | None] = {
        "cohorte_id": cohorte_id,
        "comision_id": comision_id,
        "equipo_id": equipo_id,
    }

    # El servicio publica running (no activo) como indicador del lote.
    if estado().get("running") is True:
        autor = (
            db_request.query(AutorGit)
            .options(joinedload(AutorGit.equipo))
            .filter(AutorGit.id == autor_id)
            .first()
        )
        if autor is None:
            raise HTTPException(status_code=404, detail="Autor Git no encontrado")
        return _render_fila(
            request,
            db_request,
            autor,
            estado_actual,
            filtros,
            aviso="Hay un lote en curso; reintentá al terminar",
            aviso_tipo="warning",
        )

    with SessionLocal() as db:
        autor = (
            db.query(AutorGit)
            .options(
                joinedload(AutorGit.equipo).joinedload(Equipo.comision).joinedload(Comision.cohorte),
                joinedload(AutorGit.equipo).joinedload(Equipo.estudiantes),
            )
            .filter(AutorGit.id == autor_id)
            .first()
        )
        if autor is None:
            raise HTTPException(status_code=404, detail="Autor Git no encontrado")
        commits = autor.commits
        if accion == "asignar":
            if estudiante_id is None:
                raise HTTPException(status_code=422, detail="Seleccioná un estudiante")
            estudiante = db.get(Estudiante, estudiante_id)
            if estudiante is None or estudiante.equipo_id != autor.equipo_id:
                raise HTTPException(status_code=422, detail="El estudiante debe pertenecer al mismo equipo del autor")
            autor.estudiante_id = estudiante.id
            autor.ignorado = False
            detalle = f"Asignación aplicada: {commits} commits asociados."
        elif accion == "ignorar":
            autor.estudiante_id = None
            autor.ignorado = True
            detalle = f"Autor ignorado: {commits} commits excluidos."
        else:
            autor.estudiante_id = None
            # La métrica puede inferir de nuevo un estudiante por usuario GitHub.
            # Se excluye durante el cálculo y se reactiva como pendiente después.
            autor.ignorado = True
            detalle = f"Autor desasignado: {commits} commits ahora pendientes."

        equipo = autor.equipo
        db.commit()
        resultado = calcular_equipo(db, equipo)
        if accion == "desasignar":
            autor.estudiante_id = None
            autor.ignorado = False
            db.commit()
        if resultado.get("estado") != "procesado":
            causa = resultado.get("error") or (
                "el repositorio no está descargado" if resultado.get("estado") == "no_descargado"
                else f"el cálculo terminó con estado {resultado.get('estado')}"
            )
            detalle += f" No se pudieron actualizar las métricas: {causa}."
        pendientes = _pendientes_count(db, cohorte_id, comision_id, equipo_id)
        return _render_fila(
            request,
            db,
            autor,
            estado_actual,
            filtros,
            aviso=f"{detalle} Pendientes restantes: {pendientes}.",
            resumen=f"{detalle} Pendientes restantes: {pendientes}.",
            aviso_tipo="success" if resultado.get("estado") == "procesado" else "warning",
        )


@router.post("/autores/{autor_id}/asignar", name="asignar_autor")
def asignar_autor(
    autor_id: int,
    request: Request,
    estudiante_id: int = Form(...),
    estado_actual: str = Form("pendientes"),
    cohorte_id: int | None = Form(None),
    comision_id: int | None = Form(None),
    equipo_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    return _actualizar_autor(
        autor_id, request, "asignar", estudiante_id, estado_actual,
        cohorte_id, comision_id, equipo_id, db,
    )


@router.post("/autores/{autor_id}/ignorar", name="ignorar_autor")
def ignorar_autor(
    autor_id: int,
    request: Request,
    estado_actual: str = Form("pendientes"),
    cohorte_id: int | None = Form(None),
    comision_id: int | None = Form(None),
    equipo_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    return _actualizar_autor(
        autor_id, request, "ignorar", None, estado_actual,
        cohorte_id, comision_id, equipo_id, db,
    )


@router.post("/autores/{autor_id}/desasignar", name="desasignar_autor")
def desasignar_autor(
    autor_id: int,
    request: Request,
    estado_actual: str = Form("asignados"),
    cohorte_id: int | None = Form(None),
    comision_id: int | None = Form(None),
    equipo_id: int | None = Form(None),
    db: Session = Depends(get_db),
):
    return _actualizar_autor(
        autor_id, request, "desasignar", None, estado_actual,
        cohorte_id, comision_id, equipo_id, db,
    )


def _contexto_edicion_integrante(
    request: Request,
    estudiante: Estudiante,
    equipo: Equipo,
    error: str | None = None,
    valores: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "request": request,
        "estudiante": estudiante,
        "equipo": equipo,
        "error": error,
        "valores": valores or {},
        "proceso_activo": bool(estado().get("running")),
    }


@router.get("/estudiantes/{estudiante_id}/editar", name="editar_integrante_form")
def editar_integrante_form(
    estudiante_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    estudiante = (
        db.query(Estudiante)
        .options(joinedload(Estudiante.equipo).joinedload(Equipo.comision).joinedload(Comision.cohorte))
        .filter(Estudiante.id == estudiante_id)
        .first()
    )
    if estudiante is None:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")
    return templates.TemplateResponse(
        request=request,
        name="estudiante_form.html",
        context=_contexto_edicion_integrante(request, estudiante, estudiante.equipo),
    )


@router.post("/estudiantes/{estudiante_id}/editar", name="editar_integrante_route")
def editar_integrante_route(
    estudiante_id: int,
    request: Request,
    apellido_nombre: str = Form(""),
    dni: str = Form(""),
    email: str = Form(""),
    usuario_github: str = Form(""),
    db: Session = Depends(get_db),
):
    estudiante = (
        db.query(Estudiante)
        .options(joinedload(Estudiante.equipo).joinedload(Equipo.comision).joinedload(Comision.cohorte))
        .filter(Estudiante.id == estudiante_id)
        .first()
    )
    if estudiante is None:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")
    valores = {
        "apellido_nombre": apellido_nombre,
        "dni": dni,
        "email": email,
        "usuario_github": usuario_github,
    }
    resultado = servicio_editar_integrante(
        db,
        estudiante_id,
        apellido_nombre=apellido_nombre,
        dni=dni,
        email=email,
        usuario_github=usuario_github,
    )
    if resultado.get("ok"):
        params = {
            "aviso": resultado.get("mensaje", "Integrante actualizado correctamente."),
            "requiere_calculo": "1" if resultado.get("requiere_calculo") else "0",
        }
        return RedirectResponse(
            f"/equipos/{estudiante.equipo_id}?{urlencode(params)}", status_code=303
        )
    return templates.TemplateResponse(
        request=request,
        name="estudiante_form.html",
        context=_contexto_edicion_integrante(
            request, estudiante, estudiante.equipo, resultado.get("mensaje"), valores
        ),
        status_code=422,
    )


@router.post("/estudiantes/{estudiante_id}/quitar", name="quitar_integrante_route")
def quitar_integrante_route(
    estudiante_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    estudiante = db.query(Estudiante).filter(Estudiante.id == estudiante_id).first()
    if estudiante is None:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")
    equipo_id = estudiante.equipo_id
    resultado = servicio_quitar_integrante(db, estudiante_id)
    return templates.TemplateResponse(
        request=request,
        name="_integrante_quitar_respuesta.html",
        headers={"HX-Reswap": "none"} if not resultado.get("ok") else None,
        context={
            "request": request,
            "estudiante_id": estudiante_id,
            "equipo_id": equipo_id,
            "resultado": resultado,
            "proceso_activo": bool(estado().get("running")),
            "pendientes_count": _pendientes_count(db),
        },
    )
