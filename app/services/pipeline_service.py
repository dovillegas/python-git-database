"""Ejecución de sincronizaciones y consulta de su progreso."""

from __future__ import annotations

from copy import deepcopy
from threading import Lock, Thread
from typing import Any

from git import Repo
from sqlalchemy.orm import Session, joinedload

from app.database import SessionLocal
from app.models import AutorGit, Cohorte, Comision, Equipo, MetricaProyecto
from app.services.git_cloner import repo_path, sync_repo
from app.services.metricas_service import calcular_equipo


_progress_lock = Lock()
_progress: dict[str, Any] = {
    "running": False,
    "finished": True,
    "total": 0,
    "hechos": 0,
    "descargados": 0,
    "calculados": 0,
    "errores": 0,
    "resultados": [],
    "accion": None,
    "cohorte": None,
    "comision": None,
}


def _equipos_query(db: Session, cohorte_id: int | None, comision_id: int | None):
    consulta = (
        db.query(Equipo)
        .join(Comision)
        .join(Cohorte)
        .options(joinedload(Equipo.comision).joinedload(Comision.cohorte))
    )
    if cohorte_id is not None:
        consulta = consulta.filter(Cohorte.id == cohorte_id)
    if comision_id is not None:
        consulta = consulta.filter(Comision.id == comision_id)
    return consulta.order_by(Cohorte.anio.desc(), Comision.numero, Equipo.nombre)


def estado_equipo(db: Session, equipo: Equipo) -> dict[str, Any]:
    ruta = repo_path(equipo.comision.cohorte.anio, equipo.nombre)
    descargado = ruta.is_dir() and (ruta / ".git").exists()
    error_local = None
    commit = None
    if descargado:
        try:
            commit = Repo(ruta).head.commit.hexsha[:7]
        except Exception as exc:
            error_local = f"No se pudo leer el repositorio local: {exc}"
    metrica = (
        db.query(MetricaProyecto)
        .filter(MetricaProyecto.equipo_id == equipo.id)
        .order_by(MetricaProyecto.fecha.desc(), MetricaProyecto.id.desc())
        .first()
    )
    sin_asignar = (
        db.query(AutorGit)
        .filter(
            AutorGit.equipo_id == equipo.id,
            AutorGit.estudiante_id.is_(None),
            AutorGit.ignorado.is_(False),
            AutorGit.commits > 0,
        )
        .count()
    )
    control_ok = metrica is not None and sin_asignar == 0
    if metrica is None:
        detalle_control = "Todavía no se calcularon las métricas del equipo."
    elif sin_asignar:
        detalle_control = f"Hay {sin_asignar} autor(es) con commits sin asignar a un estudiante."
    else:
        detalle_control = "Métricas calculadas; todos los autores están asignados o ignorados."
    return {
        "descargado": descargado,
        "commit": commit,
        "error_local": error_local,
        "metrica": metrica,
        "sin_asignar": sin_asignar,
        "control_ok": control_ok,
        "detalle_control": detalle_control,
    }


def estado_equipos(
    db: Session,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
) -> list[dict[str, Any]]:
    """Devuelve cada equipo junto con el estado que necesita la tabla."""
    return [
        {"equipo": equipo, "estado": estado_equipo(db, equipo)}
        for equipo in _equipos_query(db, cohorte_id, comision_id).all()
    ]


def estado() -> dict[str, Any]:
    """Devuelve una copia independiente del progreso global."""
    with _progress_lock:
        return deepcopy(_progress)


def ejecutar_equipo(db: Session, equipo: Equipo, accion: str) -> dict[str, Any]:
    resultado: dict[str, Any] = {"descargado": False, "calculado": False, "error": None, "mensaje": None}
    if accion in {"descargar", "sincronizar_calcular"}:
        sync = sync_repo(equipo.url_github, equipo.comision.cohorte.anio, equipo.nombre)
        resultado["descargado"] = sync.get("estado") in {"clonado", "actualizado", "sin_cambios"}
        if sync.get("estado") == "error":
            resultado["error"] = sync.get("error") or "No se pudo descargar el repositorio."
        elif sync.get("estado") == "vacio":
            resultado["error"] = "El repositorio remoto está vacío."
        else:
            resultado["mensaje"] = {
                "clonado": "Repositorio descargado.",
                "actualizado": "Repositorio actualizado.",
                "sin_cambios": "El repositorio ya estaba actualizado.",
            }.get(sync.get("estado"), "Descarga completada.")

    if accion in {"calcular", "sincronizar_calcular"} and resultado["error"] is None:
        calculo = calcular_equipo(db, equipo)
        resultado["calculado"] = calculo.get("estado") == "procesado"
        if calculo.get("estado") != "procesado":
            resultado["error"] = calculo.get("error") or (
                "El repositorio no está descargado." if calculo.get("estado") == "no_descargado"
                else f"No se pudieron calcular las métricas ({calculo.get('estado')})."
            )
        else:
            resultado["mensaje"] = "Métricas calculadas."
    return resultado


def _run_batch(equipo_ids: list[int], accion: str) -> None:
    db = SessionLocal()
    try:
        for equipo_id in equipo_ids:
            equipo = (
                db.query(Equipo)
                .options(joinedload(Equipo.comision).joinedload(Comision.cohorte))
                .filter(Equipo.id == equipo_id)
                .first()
            )
            if equipo is None:
                resultado = {"error": "El equipo ya no existe.", "descargado": False, "calculado": False}
                nombre = f"Equipo {equipo_id}"
                cohorte = comision = "—"
            else:
                nombre = equipo.nombre
                cohorte = equipo.comision.cohorte.anio
                comision = equipo.comision.numero
                try:
                    resultado = ejecutar_equipo(db, equipo, accion)
                except Exception as exc:
                    db.rollback()
                    resultado = {"error": str(exc), "descargado": False, "calculado": False}
            with _progress_lock:
                _progress["hechos"] += 1
                _progress["descargados"] += int(bool(resultado.get("descargado")))
                _progress["calculados"] += int(bool(resultado.get("calculado")))
                _progress["errores"] += int(bool(resultado.get("error")))
                _progress["resultados"].append(
                    {"nombre": nombre, "cohorte": cohorte, "comision": comision, **resultado}
                )
        with _progress_lock:
            _progress["finished"] = True
            _progress["running"] = False
    except Exception as exc:
        with _progress_lock:
            _progress["errores"] += 1
            _progress["resultados"].append(
                {
                    "nombre": "Proceso de sincronización",
                    "cohorte": "—",
                    "comision": "—",
                    "error": str(exc),
                    "descargado": False,
                    "calculado": False,
                }
            )
            _progress["finished"] = True
            _progress["running"] = False
    finally:
        db.close()


def ejecutar(
    accion: str,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    equipo_id: int | None = None,
) -> bool:
    """Inicia un lote en un hilo; devuelve False si ya hay otro activo."""
    db = SessionLocal()
    try:
        consulta = _equipos_query(db, cohorte_id, comision_id)
        if equipo_id is not None:
            consulta = consulta.filter(Equipo.id == equipo_id)
        equipo_ids = [equipo.id for equipo in consulta.all()]
    finally:
        db.close()

    with _progress_lock:
        if _progress["running"]:
            return False
        _progress.update(
            {
                "running": bool(equipo_ids),
                "finished": not bool(equipo_ids),
                "total": len(equipo_ids),
                "hechos": 0,
                "descargados": 0,
                "calculados": 0,
                "errores": 0,
                "resultados": [],
                "accion": accion,
                "cohorte": cohorte_id,
                "comision": comision_id,
            }
        )
        if equipo_ids:
            Thread(target=_run_batch, args=(equipo_ids, accion), name="sincronizacion").start()
    return True