from __future__ import annotations

from collections import defaultdict, deque
from copy import deepcopy
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.templating import Jinja2Templates
from git import Repo
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import Cohorte, Comision, Equipo
from app.services.git_cloner import repo_path, sync_many, sync_repo

router = APIRouter(tags=["descargas"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_progress_lock = Lock()
_progress: dict[str, Any] = {
    "running": False,
    "finished": True,
    "total": 0,
    "hechos": 0,
    "resultados": {},
    "error_general": None,
}


def _get_teams(
    db: Session,
    cohorte: int | None = None,
    comision: int | None = None,
) -> list[Equipo]:
    consulta = (
        db.query(Equipo)
        .join(Comision)
        .join(Cohorte)
        .options(joinedload(Equipo.comision).joinedload(Comision.cohorte))
    )
    if cohorte is not None:
        consulta = consulta.filter(Cohorte.id == cohorte)
    if comision is not None:
        consulta = consulta.filter(Comision.id == comision)
    return consulta.order_by(Cohorte.anio.desc(), Comision.numero, Equipo.nombre).all()


def _parse_filter(value: str | None, label: str) -> int | None:
    if value is None or not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"El filtro {label} debe ser numérico") from exc


def _local_state(equipo: Equipo) -> dict[str, Any]:
    ruta = repo_path(equipo.comision.cohorte.anio, equipo.nombre)
    if not (ruta / ".git").exists():
        return {"descargado": False, "commit": None, "fecha": None, "error": None}

    try:
        commit = Repo(ruta).head.commit
        fecha = commit.committed_datetime
        return {
            "descargado": True,
            "commit": commit.hexsha[:7],
            "fecha": fecha,
            "error": None,
        }
    except Exception as exc:
        return {
            "descargado": True,
            "commit": None,
            "fecha": None,
            "error": f"No se pudo leer el repositorio local: {exc}",
        }


def _comisiones(db: Session, cohorte: int | None) -> list[Comision]:
    consulta = db.query(Comision).options(joinedload(Comision.cohorte))
    if cohorte is not None:
        consulta = consulta.filter(Comision.cohorte_id == cohorte)
    return consulta.join(Cohorte).order_by(Cohorte.anio.desc(), Comision.numero).all()


def _progress_snapshot() -> dict[str, Any]:
    with _progress_lock:
        return deepcopy(_progress)


def _record_result(equipo_id: int, equipo: dict[str, Any], result: dict[str, Any]) -> None:
    with _progress_lock:
        _progress["resultados"][str(equipo_id)] = {
            **equipo,
            "estado": result.get("estado") or "error",
            "commit": result.get("commit"),
            "error": result.get("error"),
        }
        _progress["hechos"] += 1


def _run_batch(groups: list[list[dict[str, Any]]]) -> None:
    for group in groups:
        # sync_many informa el nombre, así que se conserva la correspondencia
        # con IDs incluso si hay etiquetas repetidas dentro de la comisión.
        ids_por_nombre: dict[str, deque[int]] = defaultdict(deque)
        equipos_por_id: dict[int, dict[str, Any]] = {}
        items: list[tuple[str, int, str]] = []
        for equipo in group:
            ids_por_nombre[equipo["nombre"]].append(equipo["id"])
            equipos_por_id[equipo["id"]] = equipo
            items.append((equipo["url"], equipo["cohorte"], equipo["nombre"]))

        def on_result(nombre: str, result: dict[str, Any]) -> None:
            with _progress_lock:
                equipo_id = ids_por_nombre[nombre].popleft() if ids_por_nombre[nombre] else None
            if equipo_id is not None:
                _record_result(equipo_id, equipos_por_id[equipo_id], result)

        try:
            sync_many(items, workers=4, on_result=on_result)
        except Exception as exc:
            with _progress_lock:
                _progress["error_general"] = str(exc)
                registrados = set(_progress["resultados"])
            for equipo in group:
                if str(equipo["id"]) not in registrados:
                    _record_result(
                        equipo["id"],
                        equipo,
                        {"estado": "error", "error": str(exc), "commit": None},
                    )

    with _progress_lock:
        registrados = set(_progress["resultados"])
        for group in groups:
            for equipo in group:
                equipo_id = equipo["id"]
                if str(equipo_id) not in registrados:
                    _progress["resultados"][str(equipo_id)] = {
                        **equipo,
                        "estado": "error",
                        "commit": None,
                        "error": _progress["error_general"] or "La sincronización no devolvió un resultado.",
                    }
                    _progress["hechos"] += 1
        _progress["running"] = False
        _progress["finished"] = True


def _group_by_commission(equipos: list[Equipo]) -> list[list[dict[str, Any]]]:
    grupos: dict[tuple[int, int, int], list[dict[str, Any]]] = {}
    for equipo in equipos:
        cohorte = equipo.comision.cohorte
        clave = (cohorte.anio, equipo.comision.numero, equipo.comision.id)
        grupos.setdefault(clave, []).append(
            {
                "id": equipo.id,
                "nombre": equipo.nombre,
                "url": equipo.url_github,
                "cohorte": cohorte.anio,
                "comision": equipo.comision.numero,
            }
        )
    return [grupos[clave] for clave in sorted(grupos)]


def _render_progress(
    request: Request,
    progreso: dict[str, Any],
    mensaje: str | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="descargas_progreso.html",
        context={"progreso": progreso, "mensaje": mensaje},
    )


@router.get("/descargas", name="pagina_descargas")
def pagina_descargas(
    request: Request,
    cohorte: str | None = None,
    comision: str | None = None,
    db: Session = Depends(get_db),
):
    cohorte_id = _parse_filter(cohorte, "cohorte")
    comision_id = _parse_filter(comision, "comisión")
    cohortes = db.query(Cohorte).order_by(Cohorte.anio.desc()).all()
    comisiones = _comisiones(db, cohorte_id)
    equipos = _get_teams(db, cohorte_id, comision_id)
    estados = {equipo.id: _local_state(equipo) for equipo in equipos}
    return templates.TemplateResponse(
        request=request,
        name="descargas.html",
        context={
            "equipos": equipos,
            "estados": estados,
            "cohortes": cohortes,
            "comisiones": comisiones,
            "cohorte_seleccionada": cohorte_id,
            "comision_seleccionada": comision_id,
            "progreso": _progress_snapshot(),
        },
    )


@router.post("/descargas/equipo/{equipo_id}", name="sincronizar_equipo")
def sincronizar_equipo(
    equipo_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    equipo = (
        db.query(Equipo)
        .options(joinedload(Equipo.comision).joinedload(Comision.cohorte))
        .filter(Equipo.id == equipo_id)
        .first()
    )
    if equipo is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")

    resultado = sync_repo(
        equipo.url_github,
        equipo.comision.cohorte.anio,
        equipo.nombre,
    )
    estado = _local_state(equipo)
    mensaje = resultado.get("error")
    if resultado.get("estado") == "error":
        mensaje = f"Error: {mensaje or 'No se pudo sincronizar el repositorio.'}"
    if resultado.get("estado") and resultado["estado"] != "error":
        mensaje = {
            "clonado": "Repositorio clonado.",
            "actualizado": "Repositorio actualizado.",
            "sin_cambios": "El repositorio ya estaba actualizado.",
            "vacio": "El repositorio remoto está vacío.",
        }.get(resultado["estado"], resultado["estado"])

    return templates.TemplateResponse(
        request=request,
        name="descargas_fila.html",
        context={"equipo": equipo, "estado": estado, "mensaje": mensaje},
    )


@router.post("/descargas/lote", name="sincronizar_lote")
def sincronizar_lote(
    request: Request,
    background_tasks: BackgroundTasks,
    cohorte: str | None = None,
    comision: str | None = None,
    db: Session = Depends(get_db),
):
    cohorte_id = _parse_filter(cohorte, "cohorte")
    comision_id = _parse_filter(comision, "comisión")
    equipos = _get_teams(db, cohorte_id, comision_id)
    grupos = _group_by_commission(equipos)

    with _progress_lock:
        if _progress["running"]:
            return _render_progress(
                request,
                deepcopy(_progress),
                mensaje="Ya hay un lote de sincronización en curso.",
            )
        _progress.update(
            {
                "running": bool(equipos),
                "finished": not bool(equipos),
                "total": len(equipos),
                "hechos": 0,
                "resultados": {},
                "error_general": None,
            }
        )

    progreso = _progress_snapshot()
    if equipos:
        background_tasks.add_task(_run_batch, grupos)
        mensaje = "Sincronización iniciada por comisión, con cuatro workers."
    else:
        mensaje = "No hay equipos para sincronizar con los filtros seleccionados."
    return _render_progress(request, progreso, mensaje=mensaje)


@router.get("/descargas/progreso", name="progreso_descargas")
def progreso_descargas(request: Request):
    return _render_progress(request, _progress_snapshot())
