"""Operaciones de mantenimiento para la base de datos y repositorios locales."""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.database import DATA_DIR, SessionLocal, engine, init_db
from app.models import (
    AutorGit,
    Cohorte,
    Comision,
    Equipo,
    Estudiante,
    MetricaEstudiante,
    MetricaProyecto,
)
from app.services.git_cloner import REPOS_DIR


def _pipeline_activo() -> tuple[bool, str | None]:
    """Reconoce tanto la clave solicitada como la clave actual del pipeline."""
    try:
        from app.services import pipeline_service

        status = pipeline_service.estado()
        return status.get("activo") is True or status.get("running") is True, None
    except Exception as exc:
        return True, f"No se pudo verificar el estado del pipeline: {exc}"


def _rechazo_pipeline() -> dict[str, Any] | None:
    activo, error = _pipeline_activo()
    if error:
        return {"ok": False, "mensaje": error}
    if activo:
        return {"ok": False, "mensaje": "No se puede realizar mantenimiento mientras el pipeline está activo."}
    return None


def _conteo(db: Session, modelo: Any) -> int:
    return int(db.query(func.count(modelo.id)).scalar() or 0)


def _metricas_por_cohorte(db: Session, cohorte_id: int) -> int:
    proyectos = (
        db.query(func.count(MetricaProyecto.id))
        .join(Equipo, MetricaProyecto.equipo_id == Equipo.id)
        .join(Comision, Equipo.comision_id == Comision.id)
        .filter(Comision.cohorte_id == cohorte_id)
        .scalar()
        or 0
    )
    estudiantes = (
        db.query(func.count(MetricaEstudiante.id))
        .join(Estudiante, MetricaEstudiante.estudiante_id == Estudiante.id)
        .join(Equipo, Estudiante.equipo_id == Equipo.id)
        .join(Comision, Equipo.comision_id == Comision.id)
        .filter(Comision.cohorte_id == cohorte_id)
        .scalar()
        or 0
    )
    return int(proyectos + estudiantes)


def _estadisticas_repositorios() -> tuple[int, int]:
    """Devuelve directorios descendientes y bytes de archivos, sin seguir symlinks."""
    if not REPOS_DIR.exists() or not REPOS_DIR.is_dir():
        return 0, 0

    carpetas = 0
    bytes_totales = 0
    for path in REPOS_DIR.rglob("*"):
        if path.is_symlink():
            continue
        if path.is_dir():
            carpetas += 1
        elif path.is_file():
            try:
                bytes_totales += path.stat().st_size
            except OSError:
                continue
    return carpetas, bytes_totales


def resumen() -> dict[str, Any]:
    rechazo = _rechazo_pipeline()
    if rechazo:
        return rechazo

    db = SessionLocal()
    try:
        carpetas, bytes_totales = _estadisticas_repositorios()
        ruta_db = DATA_DIR / "app.db"
        tamano_db = ruta_db.stat().st_size if ruta_db.is_file() else 0
        cohortes = []
        for cohorte in db.query(Cohorte).order_by(Cohorte.anio).all():
            comisiones = (
                db.query(Comision)
                .filter(Comision.cohorte_id == cohorte.id)
                .all()
            )
            equipos = (
                db.query(Equipo)
                .join(Comision, Equipo.comision_id == Comision.id)
                .filter(Comision.cohorte_id == cohorte.id)
                .count()
            )
            estudiantes = (
                db.query(Estudiante)
                .join(Equipo, Estudiante.equipo_id == Equipo.id)
                .join(Comision, Equipo.comision_id == Comision.id)
                .filter(Comision.cohorte_id == cohorte.id)
                .count()
            )
            autores = (
                db.query(AutorGit)
                .join(Equipo, AutorGit.equipo_id == Equipo.id)
                .join(Comision, Equipo.comision_id == Comision.id)
                .filter(Comision.cohorte_id == cohorte.id)
                .count()
            )
            cohortes.append(
                {
                    "anio": cohorte.anio,
                    "comisiones": len(comisiones),
                    "equipos": equipos,
                    "estudiantes": estudiantes,
                    "filas_metricas": _metricas_por_cohorte(db, cohorte.id),
                    "autores_git": autores,
                }
            )

        metricas_proyecto = _conteo(db, MetricaProyecto)
        metricas_estudiante = _conteo(db, MetricaEstudiante)
        return {
            "ok": True,
            "mensaje": "Resumen de mantenimiento generado.",
            "cohortes": _conteo(db, Cohorte),
            "comisiones": _conteo(db, Comision),
            "equipos": _conteo(db, Equipo),
            "estudiantes": _conteo(db, Estudiante),
            "filas_metricas": metricas_proyecto + metricas_estudiante,
            "metricas_proyecto": metricas_proyecto,
            "metricas_estudiante": metricas_estudiante,
            "autores_git": _conteo(db, AutorGit),
            "carpetas_repos": carpetas,
            "tamano_repos_mb": round(bytes_totales / (1024 * 1024), 2),
            "tamano_db_mb": round(tamano_db / (1024 * 1024), 2),
            "tamano_total_mb": round((bytes_totales + tamano_db) / (1024 * 1024), 2),
            "por_cohorte": cohortes,
            "cohortes_detalle": cohortes,
        }
    except Exception as exc:
        return {"ok": False, "mensaje": f"No se pudo generar el resumen: {exc}"}
    finally:
        db.close()


def respaldar_base() -> dict[str, Any]:
    rechazo = _rechazo_pipeline()
    if rechazo:
        return rechazo

    try:
        db_path = DATA_DIR / "app.db"
        if not db_path.is_file():
            return {"ok": False, "mensaje": f"No existe la base de datos: {db_path}"}

        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        destino = backup_dir / f"app_{timestamp}.db"
        shutil.copy2(db_path, destino)

        respaldos = sorted(
            backup_dir.glob("app_????????_??????.db"),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
            reverse=True,
        )
        for obsoleto in respaldos[5:]:
            obsoleto.unlink()
        return {"ok": True, "mensaje": "Respaldo creado.", "ruta": str(destino)}
    except Exception as exc:
        return {"ok": False, "mensaje": f"No se pudo respaldar la base de datos: {exc}"}


def borrar_metricas(db: Session) -> dict[str, Any]:
    rechazo = _rechazo_pipeline()
    if rechazo:
        return rechazo

    try:
        proyectos = db.query(MetricaProyecto).delete(synchronize_session=False)
        estudiantes = db.query(MetricaEstudiante).delete(synchronize_session=False)
        db.commit()
        return {
            "ok": True,
            "mensaje": "Se borraron las métricas; los autores Git se conservaron.",
            "metricas_proyecto": proyectos,
            "metricas_estudiante": estudiantes,
        }
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudieron borrar las métricas: {exc}"}


def _borrar_datos_sin_respaldo(db: Session) -> dict[str, Any]:
    try:
        db.close()
        from app.database import Base

        Base.metadata.drop_all(bind=engine)
        init_db()
        return {"ok": True, "mensaje": "La base fue reinicializada; el archivo app.db se conservó."}
    except Exception as exc:
        return {"ok": False, "mensaje": f"No se pudieron reinicializar los datos: {exc}"}


def borrar_datos(db: Session) -> dict[str, Any]:
    rechazo = _rechazo_pipeline()
    if rechazo:
        return rechazo
    respaldo = respaldar_base()
    if not respaldo.get("ok"):
        return respaldo

    resultado = _borrar_datos_sin_respaldo(db)
    resultado["respaldo"] = respaldo.get("ruta")
    return resultado


def _esta_dentro(ruta: Path, raiz: Path) -> bool:
    ruta_resuelta = ruta.resolve()
    raiz_resuelta = raiz.resolve()
    return ruta_resuelta != raiz_resuelta and ruta_resuelta.is_relative_to(raiz_resuelta)


def _eliminar_entrada(ruta: Path, raiz: Path) -> int:
    """Elimina una entrada contenida en la raíz y devuelve bytes de archivos borrados."""
    if not _esta_dentro(ruta, raiz):
        raise ValueError(f"Ruta fuera del directorio permitido: {ruta}")
    if ruta.is_symlink():
        ruta.unlink()
        return 0
    if ruta.is_dir():
        liberados = 0
        for hijo in ruta.iterdir():
            liberados += _eliminar_entrada(hijo, raiz)
        ruta.rmdir()
        return liberados
    if ruta.is_file():
        tamano = ruta.stat().st_size
        ruta.unlink()
        return tamano
    ruta.unlink(missing_ok=True)
    return 0


def borrar_repos(cohorte_anio: int | str | None = None) -> dict[str, Any]:
    rechazo = _rechazo_pipeline()
    if rechazo:
        return rechazo

    liberados = 0
    try:
        data_root = DATA_DIR.resolve()
        raiz = REPOS_DIR.resolve()
        if (
            REPOS_DIR.name != "repos"
            or REPOS_DIR.is_symlink()
            or REPOS_DIR.parent.resolve() != data_root
            or raiz == data_root
            or not raiz.is_relative_to(data_root)
        ):
            raise ValueError("REPOS_DIR no está estrictamente dentro de DATA_DIR.")

        destino = raiz if cohorte_anio is None else REPOS_DIR / str(cohorte_anio)
        if cohorte_anio is None:
            entradas = list(raiz.iterdir()) if raiz.exists() else []
        else:
            if not _esta_dentro(destino, raiz):
                raise ValueError("La ruta de cohorte no está estrictamente dentro de REPOS_DIR.")
            entradas = list(destino.iterdir()) if destino.is_dir() else []

        for entrada in entradas:
            liberados += _eliminar_entrada(entrada, raiz)
        return {
            "ok": True,
            "mensaje": "Contenido de repositorios eliminado.",
            "bytes_liberados": liberados,
        }
    except Exception as exc:
        return {
            "ok": False,
            "mensaje": f"No se pudieron borrar los repositorios: {exc}",
            "bytes_liberados": liberados,
        }


def borrar_todo(db: Session) -> dict[str, Any]:
    datos = borrar_datos(db)
    if not datos.get("ok"):
        return datos

    repos = borrar_repos()
    if not repos.get("ok"):
        return {
            "ok": False,
            "mensaje": f"La base se reinicializó, pero falló el borrado de repositorios: {repos['mensaje']}",
            "respaldo": datos.get("respaldo"),
            "bytes_liberados": repos.get("bytes_liberados", 0),
        }
    return {
        "ok": True,
        "mensaje": "Base de datos y repositorios reinicializados.",
        "respaldo": datos.get("respaldo"),
        "bytes_liberados": repos.get("bytes_liberados", 0),
    }