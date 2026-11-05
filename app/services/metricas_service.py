"""Persistencia y consultas de métricas Git calculadas desde repos locales."""

from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    AutorGit,
    Cohorte,
    Comision,
    Equipo,
    MetricaEstudiante,
    MetricaProyecto,
)
from app.services.git_cloner import repo_path
from app.services.local_metrics import metricas_locales

DIAS_SIN_ACTIVIDAD = 14
UMBRAL_DESBALANCE = 0.60


def evaluar_semaforo(
    indicadores: dict[str, Any],
    metrica: MetricaProyecto | None,
    estado_repo: dict[str, Any],
    estado_salud: str | None,
    autores_sin_asignar: int,
) -> tuple[str, list[dict[str, str]]]:
    """Aplica los criterios actuales del semáforo y explica cada causa activa."""
    motivos: list[dict[str, str]] = []
    no_descargado = not estado_repo.get("descargado", False)
    error_local = bool(estado_repo.get("error_local"))
    error_salud = estado_salud == "error"
    if no_descargado:
        motivos.append({"icono": "📦", "texto": "Repositorio no descargado"})
    if error_local or error_salud:
        motivos.append({"icono": "⚠️", "texto": "Error en el repositorio local"})
    if autores_sin_asignar:
        motivos.append({"icono": "👤", "texto": f"{autores_sin_asignar} autor(es) con commits sin asignar"})

    actividad = (
        metrica is not None
        and metrica.dias_sin_actividad is not None
        and metrica.dias_sin_actividad >= DIAS_SIN_ACTIVIDAD
    )
    desbalance = (
        metrica is not None
        and indicadores.get("total_commits", 0) > 0
        and indicadores.get("desbalance", 0) / 100 >= UMBRAL_DESBALANCE
    )
    integrantes_sin_commits = (
        sum(1 for item in indicadores.get("integrantes", []) if item.get("commits", 0) == 0)
        if metrica is not None
        else 0
    )
    if actividad:
        motivos.append({"icono": "⏳", "texto": f"Sin actividad durante {metrica.dias_sin_actividad} días"})
    if desbalance:
        motivos.append({"icono": "⚖️", "texto": f"Desbalance igual o superior al {UMBRAL_DESBALANCE * 100:g}%"})
    if integrantes_sin_commits:
        motivos.append({"icono": "0️⃣", "texto": f"{integrantes_sin_commits} integrante(s) sin commits"})
    if metrica is None:
        motivos.append({"icono": "🧮", "texto": "Métricas sin calcular"})

    if no_descargado or error_local or error_salud or autores_sin_asignar:
        semaforo = "crítico"
    elif actividad or desbalance or integrantes_sin_commits or metrica is None:
        semaforo = "advertencia"
    else:
        semaforo = "ok"
    return semaforo, motivos


def _fecha_snapshot() -> tuple[date, datetime, datetime]:
    dia = datetime.now(timezone.utc).date()
    inicio = datetime.combine(dia, time.min)
    return dia, inicio, datetime.combine(dia, time.max)


def _datetime(valor: str | datetime | None) -> datetime | None:
    if not valor:
        return None
    if isinstance(valor, datetime):
        resultado = valor
    else:
        resultado = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    if resultado.tzinfo is not None:
        resultado = resultado.astimezone(timezone.utc).replace(tzinfo=None)
    return resultado


def _equipo_repo_descargado(equipo: Equipo) -> bool:
    cohorte = equipo.comision.cohorte
    ruta = repo_path(cohorte.anio, equipo.nombre)
    return ruta.is_dir() and (ruta / ".git").exists()


def _alias_equipo(db: Session, equipo_id: int) -> dict[str, int | None]:
    alias: dict[str, int | None] = {}
    autores = db.query(AutorGit).filter(AutorGit.equipo_id == equipo_id).all()
    for autor in autores:
        if autor.ignorado:
            alias[autor.email.strip().lower()] = None
        elif autor.estudiante_id is not None:
            alias[autor.email.strip().lower()] = autor.estudiante_id
    return alias


def _buscar_por_dia(
    db: Session,
    modelo: type[MetricaProyecto] | type[MetricaEstudiante],
    clave: str,
    valor: int,
    inicio: datetime,
    fin: datetime,
) -> MetricaProyecto | MetricaEstudiante | None:
    filas = (
        db.query(modelo)
        .filter(
            getattr(modelo, clave) == valor,
            modelo.fecha >= inicio,
            modelo.fecha <= fin,
        )
        .order_by(modelo.id.desc())
        .all()
    )
    if len(filas) > 1:
        for duplicada in filas[1:]:
            db.delete(duplicada)
    return filas[0] if filas else None


def _json(datos: Any) -> str:
    return json.dumps(datos, ensure_ascii=False, default=str)


def calcular_equipo(db: Session, equipo: Equipo) -> dict[str, Any]:
    """Calcula y guarda la foto diaria de actividad de un equipo."""
    equipo_id = equipo.id
    try:
        if not _equipo_repo_descargado(equipo):
            equipo.estado_salud = "no_descargado"
            db.commit()
            return {"estado": "no_descargado", "equipo_id": equipo_id}

        comision = equipo.comision
        cohorte_anio = comision.cohorte.anio
        alias = _alias_equipo(db, equipo_id)
        metricas = metricas_locales(
            cohorte_anio,
            equipo.nombre,
            equipo.estudiantes,
            alias=alias,
        )
        if metricas.get("error"):
            db.rollback()
            return {
                "estado": "error",
                "equipo_id": equipo_id,
                "error": str(metricas["error"]),
            }
        if equipo.estado_salud == "no_descargado":
            equipo.estado_salud = None

        dia, inicio, fin = _fecha_snapshot()
        proyecto = _buscar_por_dia(
            db, MetricaProyecto, "equipo_id", equipo_id, inicio, fin
        )
        if proyecto is None:
            proyecto = MetricaProyecto(equipo_id=equipo_id, fecha=inicio)
            db.add(proyecto)
        resumen = metricas["equipo"]
        proyecto.fecha = inicio
        proyecto.total_commits = resumen["commits"]
        proyecto.ultimo_commit = _datetime(resumen["ultimo_commit"])
        proyecto.dias_sin_actividad = resumen["dias_sin_actividad"]
        proyecto.lineas_agregadas = resumen["lineas_agregadas"]
        proyecto.lineas_eliminadas = resumen["lineas_eliminadas"]
        proyecto.archivos_total = resumen["archivos"]
        proyecto.detalle_json = _json(
            {
                "commits_por_semana": resumen["commits_por_semana"],
                "commits_por_hora": resumen["commits_por_hora"],
                "commits_ignorados": resumen["commits_ignorados"],
                "autores_sin_asignar": metricas["autores_sin_asignar"],
            }
        )

        metricas_estudiantes = {
            fila["estudiante_id"]: fila
            for fila in metricas["estudiantes"]
            if fila.get("estudiante_id") is not None
        }
        for estudiante in equipo.estudiantes:
            fila = metricas_estudiantes.get(estudiante.id, {})
            registro = _buscar_por_dia(
                db,
                MetricaEstudiante,
                "estudiante_id",
                estudiante.id,
                inicio,
                fin,
            )
            if registro is None:
                registro = MetricaEstudiante(
                    estudiante_id=estudiante.id,
                    fecha=inicio,
                )
                db.add(registro)
            registro.fecha = inicio
            registro.commits_count = fila.get("commits", 0)
            registro.lineas_agregadas = fila.get("lineas_agregadas", 0)
            registro.lineas_eliminadas = fila.get("lineas_eliminadas", 0)
            registro.archivos_tocados = fila.get("archivos_tocados", 0)
            registro.dias_activos = fila.get("dias_activos", 0)
            registro.primer_commit = _datetime(fila.get("primer_commit"))
            registro.ultimo_commit = _datetime(fila.get("ultimo_commit"))
            registro.detalle_json = _json(
                {
                    "commits_por_semana": fila.get("commits_por_semana", {}),
                    "commits_por_hora": fila.get("commits_por_hora", {}),
                }
            )

        autores_actuales = {
            autor.email.strip().lower(): autor
            for autor in db.query(AutorGit)
            .filter(AutorGit.equipo_id == equipo_id)
            .all()
        }
        # Los autores que ya no aparecen en el historial no deben seguir
        # sumando commits en el resumen; las asignaciones manuales se conservan.
        for autor in autores_actuales.values():
            autor.commits = 0
        for dato in metricas.get("autores", []):
            email = dato["email"].strip().lower()
            if not email:
                continue
            autor = autores_actuales.get(email)
            if autor is None:
                autor = AutorGit(
                    equipo_id=equipo_id,
                    email=email,
                    nombre_git=dato["nombre_git"],
                    commits=dato["commits"],
                    estudiante_id=dato.get("estudiante_id"),
                    ignorado=bool(dato.get("ignorado", False)),
                )
                db.add(autor)
            else:
                autor.nombre_git = dato["nombre_git"]
                autor.commits = dato["commits"]
                # Completa una asignación inferida, pero nunca cambia una
                # asignación o exclusión manual previamente definida.
                if not autor.ignorado and autor.estudiante_id is None:
                    autor.estudiante_id = dato.get("estudiante_id")

        db.commit()
        return {
            "estado": "procesado",
            "equipo_id": equipo_id,
            "fecha": dia.isoformat(),
            "commits": resumen["commits"],
            "autores_sin_asignar": len(metricas["autores_sin_asignar"]),
        }
    except Exception as exc:
        db.rollback()
        return {"estado": "error", "equipo_id": equipo_id, "error": str(exc)}


def _filtrar_equipos(
    db: Session,
    cohorte: Cohorte | int | str | None = None,
    comision: Comision | int | None = None,
) -> list[Equipo]:
    consulta = db.query(Equipo).join(Comision).join(Cohorte)
    if cohorte is not None:
        if isinstance(cohorte, Cohorte):
            consulta = consulta.filter(Cohorte.id == cohorte.id)
        else:
            try:
                valor = int(cohorte)
                consulta = consulta.filter(or_(Cohorte.id == valor, Cohorte.anio == valor))
            except (TypeError, ValueError):
                consulta = consulta.filter(Cohorte.anio == str(cohorte))
    if comision is not None:
        consulta = consulta.filter(
            Comision.id == (comision.id if isinstance(comision, Comision) else comision)
        )
    return consulta.order_by(Cohorte.anio, Comision.numero, Equipo.nombre).all()


def calcular_todos(
    db: Session,
    cohorte: Cohorte | int | str | None = None,
    comision: Comision | int | None = None,
) -> dict[str, Any]:
    """Procesa equipos individualmente; un fallo no detiene el resto del lote."""
    resultados = []
    for equipo in _filtrar_equipos(db, cohorte, comision):
        resultados.append(calcular_equipo(db, equipo))
    return {
        "procesados": sum(r["estado"] == "procesado" for r in resultados),
        "sin_repo": sum(r["estado"] == "no_descargado" for r in resultados),
        "con_error": sum(r["estado"] == "error" for r in resultados),
        "resultados": resultados,
    }


def resumen_global(
    db: Session,
    cohorte: Cohorte | int | str | None = None,
    comision: Comision | int | None = None,
) -> dict[str, Any]:
    """Totales y alertas actuales para el panel, limitados al filtro indicado."""
    equipos = _filtrar_equipos(db, cohorte, comision)
    ids = [equipo.id for equipo in equipos]
    if not ids:
        return {
            "totales": {
                "equipos": 0,
                "commits": 0,
                "lineas_agregadas": 0,
                "lineas_eliminadas": 0,
                "autores_sin_asignar": 0,
                "commits_sin_asignar": 0,
                "sin_descargar": 0,
            },
            "alertas": [],
        }

    ultima_por_equipo: dict[int, MetricaProyecto] = {}
    for equipo_id in ids:
        ultima = (
            db.query(MetricaProyecto)
            .filter(MetricaProyecto.equipo_id == equipo_id)
            .order_by(MetricaProyecto.fecha.desc(), MetricaProyecto.id.desc())
            .first()
        )
        if ultima is not None:
            ultima_por_equipo[equipo_id] = ultima

    autores = db.query(AutorGit).filter(AutorGit.equipo_id.in_(ids)).all()
    sin_asignar = [
        a
        for a in autores
        if a.estudiante_id is None and not a.ignorado and a.commits > 0
    ]
    no_descargados = [e for e in equipos if e.estado_salud == "no_descargado"]
    commits = sum(m.total_commits for m in ultima_por_equipo.values())
    lineas_agregadas = sum(m.lineas_agregadas or 0 for m in ultima_por_equipo.values())
    lineas_eliminadas = sum(m.lineas_eliminadas or 0 for m in ultima_por_equipo.values())

    alertas: list[dict[str, Any]] = []
    if sin_asignar:
        afectados = sum(a.commits for a in sin_asignar)
        alertas.append(
            {
                "tipo": "autores_sin_asignar",
                "cantidad": len(sin_asignar),
                "commits_afectados": afectados,
                "mensaje": f"{len(sin_asignar)} autores sin asignar ({afectados} commits).",
            }
        )
    if no_descargados:
        alertas.append(
            {
                "tipo": "repositorios_no_descargados",
                "cantidad": len(no_descargados),
                "mensaje": f"{len(no_descargados)} equipos no tienen el repositorio descargado.",
            }
        )
    sin_actividad = [
        m for m in ultima_por_equipo.values()
        if m.dias_sin_actividad is not None and m.dias_sin_actividad >= 14
    ]
    if sin_actividad:
        alertas.append(
            {
                "tipo": "sin_actividad",
                "cantidad": len(sin_actividad),
                "mensaje": f"{len(sin_actividad)} equipos llevan 14 días o más sin actividad.",
            }
        )

    return {
        "totales": {
            "equipos": len(equipos),
            "commits": commits,
            "lineas_agregadas": lineas_agregadas,
            "lineas_eliminadas": lineas_eliminadas,
            "autores_sin_asignar": len(sin_asignar),
            "commits_sin_asignar": sum(a.commits for a in sin_asignar),
            "sin_descargar": len(no_descargados),
        },
        "alertas": alertas,
    }


def indicadores_equipo(
    db: Session,
    equipo_id: int,
    *,
    estado_repo: dict[str, Any] | None = None,
    autores_sin_asignar: int | None = None,
) -> dict[str, Any]:
    """Aporte porcentual, nivel de desbalance e inactividad por integrante."""
    equipo = db.get(Equipo, equipo_id)
    if equipo is None:
        return {
            "equipo_id": equipo_id,
            "integrantes": [],
            "desbalance": 0.0,
            "dias_sin_actividad": None,
            "motivos": [],
        }

    ultima_por_estudiante: dict[int, MetricaEstudiante] = {}
    for estudiante in equipo.estudiantes:
        ultima = (
            db.query(MetricaEstudiante)
            .filter(MetricaEstudiante.estudiante_id == estudiante.id)
            .order_by(MetricaEstudiante.fecha.desc(), MetricaEstudiante.id.desc())
            .first()
        )
        if ultima is not None:
            ultima_por_estudiante[estudiante.id] = ultima

    total_commits = sum(m.commits_count for m in ultima_por_estudiante.values())
    hoy = datetime.now(timezone.utc).date()
    integrantes = []
    aportes = []
    for estudiante in equipo.estudiantes:
        metrica = ultima_por_estudiante.get(estudiante.id)
        commits = metrica.commits_count if metrica else 0
        aporte = (commits / total_commits * 100) if total_commits else 0.0
        aportes.append(aporte)
        ultimo = metrica.ultimo_commit if metrica else None
        dias = (hoy - ultimo.date()).days if ultimo else None
        integrantes.append(
            {
                "estudiante_id": estudiante.id,
                "estudiante": estudiante.apellido_nombre,
                "commits": commits,
                "aporte_pct": round(aporte, 2),
                "dias_sin_actividad": dias,
            }
        )

    desbalance = max(aportes, default=0.0) - min(aportes, default=0.0)
    ultima_proyecto = (
        db.query(MetricaProyecto)
        .filter(MetricaProyecto.equipo_id == equipo_id)
        .order_by(MetricaProyecto.fecha.desc(), MetricaProyecto.id.desc())
        .first()
    )
    dias_equipo = ultima_proyecto.dias_sin_actividad if ultima_proyecto else None
    resultado = {
        "equipo_id": equipo_id,
        "total_commits": total_commits,
        "integrantes": integrantes,
        "desbalance": round(desbalance, 2),
        "dias_sin_actividad": dias_equipo,
    }
    dominante = max(integrantes, key=lambda item: item["aporte_pct"], default=None) if total_commits else None
    resultado["integrante_dominante"] = dominante["estudiante"] if dominante else None
    resultado["integrante_dominante_id"] = dominante["estudiante_id"] if dominante else None
    resultado["dominante_pct"] = dominante["aporte_pct"] if dominante else 0.0
    if estado_repo is None:
        estado_repo = {"descargado": _equipo_repo_descargado(equipo)}
    if autores_sin_asignar is None:
        autores_sin_asignar = (
            db.query(AutorGit)
            .filter(
                AutorGit.equipo_id == equipo_id,
                AutorGit.estudiante_id.is_(None),
                AutorGit.ignorado.is_(False),
                AutorGit.commits > 0,
            )
            .count()
        )
    resultado["semaforo"], resultado["motivos"] = evaluar_semaforo(
        resultado,
        ultima_proyecto,
        estado_repo,
        equipo.estado_salud,
        autores_sin_asignar,
    )
    return resultado
