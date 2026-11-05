from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates
from fastapi.responses import RedirectResponse
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.models import AutorGit, Cohorte, Comision, Equipo, Estudiante, MetricaEstudiante, MetricaProyecto
from app.services.metricas_service import (
    DIAS_SIN_ACTIVIDAD,
    UMBRAL_DESBALANCE,
    evaluar_semaforo,
    indicadores_equipo,
    resumen_global,
)
from app.services.evaluacion_service import evaluar_equipo
from app.services.pipeline_service import ejecutar, estado, estado_equipos
from app.services.equipos_service import (
    agregar_integrante as servicio_agregar_integrante,
    crear_equipo as servicio_crear_equipo,
    editar_equipo as servicio_editar_equipo,
    mover_integrante as servicio_mover_integrante,
)


router = APIRouter(tags=["panel"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))

_ALERTAS = {
    "sin_actividad",
    "desbalance",
    "sin_asignar",
    "no_descargado",
    "integrantes_sin_commits",
}
_ORDENES = {"nombre", "comision", "commits", "ultimo_commit", "dias_sin_actividad", "dominante"}


def _parse_id(value: int | None, label: str) -> int | None:
    if value is not None and value < 1:
        raise HTTPException(status_code=422, detail=f"El filtro {label} debe ser positivo")
    return value


def _form_id(value: str, label: str) -> tuple[int | None, str | None]:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, f"Seleccioná {label}."
    if parsed < 1:
        return None, f"Seleccioná {label}."
    return parsed, None


def _equipo_form_context(
    db: Session,
    request: Request,
    *,
    equipo: Equipo | None = None,
    valores: dict[str, Any] | None = None,
    error: str | None = None,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
) -> dict[str, Any]:
    valores = valores or {}
    if equipo is not None:
        comision_actual = equipo.comision
        cohorte_id = cohorte_id or comision_actual.cohorte_id
        comision_id = comision_id or equipo.comision_id
        valores = {
            "nombre": equipo.nombre,
            "proyecto_nombre": equipo.nombre_proyecto or "",
            "url_github": equipo.url_github,
            **valores,
        }
    cohortes = db.query(Cohorte).order_by(Cohorte.anio.desc()).all()
    comisiones_query = db.query(Comision).options(joinedload(Comision.cohorte))
    if cohorte_id is not None:
        comisiones_query = comisiones_query.filter(Comision.cohorte_id == cohorte_id)
    comisiones = comisiones_query.join(Cohorte).order_by(Cohorte.anio.desc(), Comision.numero).all()
    return {
        "request": request,
        "equipo": equipo,
        "valores": valores,
        "cohortes": cohortes,
        "comisiones": comisiones,
        "cohorte_seleccionada": cohorte_id,
        "comision_seleccionada": comision_id,
        "error": error,
        "proceso_activo": bool(estado().get("running")),
    }


def _detalle_metrica(metrica: MetricaProyecto | MetricaEstudiante | None) -> dict[str, Any]:
    if metrica is None or not metrica.detalle_json:
        return {}
    try:
        detalle = json.loads(metrica.detalle_json)
    except (TypeError, ValueError):
        return {}
    return detalle if isinstance(detalle, dict) else {}


def _serie_metrica(detalle: dict[str, Any], clave: str, horas: bool = False) -> dict[str, int]:
    serie = detalle.get(clave)
    if not isinstance(serie, dict):
        return {}
    resultado: dict[str, int] = {}
    for etiqueta, valor in serie.items():
        try:
            resultado[str(etiqueta)] = int(valor)
        except (TypeError, ValueError, OverflowError):
            continue
    if horas:
        return {str(hora): resultado.get(str(hora), 0) for hora in range(24)}
    return dict(sorted(resultado.items()))


def _filters_context(db: Session, cohorte_id: int | None, comision_id: int | None) -> dict[str, Any]:
    cohortes = db.query(Cohorte).order_by(Cohorte.anio.desc()).all()
    consulta = db.query(Comision).options(joinedload(Comision.cohorte)).join(Cohorte)
    if cohorte_id is not None:
        consulta = consulta.filter(Comision.cohorte_id == cohorte_id)
    comisiones = consulta.order_by(Cohorte.anio.desc(), Comision.numero).all()
    if comision_id is not None and not any(item.id == comision_id for item in comisiones):
        comision_id = None
    return {
        "cohortes": cohortes,
        "comisiones": comisiones,
        "cohorte_seleccionada": cohorte_id,
        "comision_seleccionada": comision_id,
    }


def _equipos_filtrados(db: Session, cohorte_id: int | None, comision_id: int | None) -> list[Equipo]:
    consulta = (
        db.query(Equipo)
        .join(Comision)
        .join(Cohorte)
        .options(
            joinedload(Equipo.comision).joinedload(Comision.cohorte),
            joinedload(Equipo.estudiantes),
        )
    )
    if cohorte_id is not None:
        consulta = consulta.filter(Cohorte.id == cohorte_id)
    if comision_id is not None:
        consulta = consulta.filter(Comision.id == comision_id)
    return consulta.order_by(Cohorte.anio.desc(), Comision.numero, Equipo.nombre).all()


def _ultimas_metricas(db: Session, equipo_ids: list[int]) -> dict[int, MetricaProyecto]:
    """Carga en una consulta las métricas más recientes de todos los equipos."""
    if not equipo_ids:
        return {}
    ordenadas = (
        db.query(
            MetricaProyecto.id.label("metrica_id"),
            func.row_number()
            .over(
                partition_by=MetricaProyecto.equipo_id,
                order_by=(MetricaProyecto.fecha.desc(), MetricaProyecto.id.desc()),
            )
            .label("posicion"),
        )
        .filter(MetricaProyecto.equipo_id.in_(equipo_ids))
        .subquery()
    )
    filas = (
        db.query(MetricaProyecto)
        .join(ordenadas, MetricaProyecto.id == ordenadas.c.metrica_id)
        .filter(ordenadas.c.posicion == 1)
        .all()
    )
    return {metrica.equipo_id: metrica for metrica in filas}


def _indicadores_en_lote(
    db: Session,
    equipos: list[Equipo],
    metricas: dict[int, MetricaProyecto],
) -> dict[int, dict[str, Any]]:
    """Construye el mismo esquema que indicadores_equipo desde snapshots en lote."""
    estudiante_a_equipo = {
        estudiante.id: equipo.id
        for equipo in equipos
        for estudiante in equipo.estudiantes
    }
    if not estudiante_a_equipo:
        return {
            equipo.id: {
                "equipo_id": equipo.id,
                "total_commits": 0,
                "integrantes": [],
                "desbalance": 0.0,
                "dias_sin_actividad": metricas[equipo.id].dias_sin_actividad if equipo.id in metricas else None,
            }
            for equipo in equipos
        }

    ultimas: dict[int, MetricaEstudiante] = {}
    filas = (
        db.query(MetricaEstudiante)
        .filter(MetricaEstudiante.estudiante_id.in_(estudiante_a_equipo))
        .order_by(MetricaEstudiante.fecha.desc(), MetricaEstudiante.id.desc())
        .all()
    )
    for metrica in filas:
        ultimas.setdefault(metrica.estudiante_id, metrica)

    hoy = datetime.now(timezone.utc).date()
    resultados: dict[int, dict[str, Any]] = {}
    for equipo in equipos:
        datos_integrantes = []
        aportes = []
        total_commits = sum(
            ultimas[estudiante.id].commits_count
            for estudiante in equipo.estudiantes
            if estudiante.id in ultimas
        )
        for estudiante in equipo.estudiantes:
            metrica = ultimas.get(estudiante.id)
            commits = metrica.commits_count if metrica else 0
            aporte = commits / total_commits * 100 if total_commits else 0.0
            aportes.append(aporte)
            ultimo = metrica.ultimo_commit if metrica else None
            dias = (hoy - ultimo.date()).days if ultimo else None
            datos_integrantes.append(
                {
                    "estudiante_id": estudiante.id,
                    "estudiante": estudiante.apellido_nombre,
                    "commits": commits,
                    "aporte_pct": round(aporte, 2),
                    "dias_sin_actividad": dias,
                }
            )
        resultados[equipo.id] = {
            "equipo_id": equipo.id,
            "total_commits": total_commits,
            "integrantes": datos_integrantes,
            "desbalance": round(max(aportes, default=0.0) - min(aportes, default=0.0), 2),
            "dias_sin_actividad": metricas[equipo.id].dias_sin_actividad if equipo.id in metricas else None,
        }
    return resultados


def _datos_panel(
    db: Session,
    equipos: list[Equipo],
    metricas: dict[int, MetricaProyecto] | None = None,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
) -> dict[str, Any]:
    ids = [equipo.id for equipo in equipos]
    metricas = metricas if metricas is not None else _ultimas_metricas(db, ids)
    estados = estado_equipos(db, cohorte_id, comision_id) if ids else []
    estados_por_id = {item["equipo"].id: item["estado"] for item in estados if item["equipo"].id in ids}

    autores_por_equipo: dict[int, list[AutorGit]] = defaultdict(list)
    commits_ignorados_por_equipo: dict[int, int] = defaultdict(int)
    if ids:
        for autor in db.query(AutorGit).filter(AutorGit.equipo_id.in_(ids)).all():
            if autor.ignorado:
                commits_ignorados_por_equipo[autor.equipo_id] += autor.commits
            elif autor.estudiante_id is None and autor.commits > 0:
                autores_por_equipo[autor.equipo_id].append(autor)

    indicadores_por_id = _indicadores_en_lote(db, equipos, metricas)

    filas = []
    for equipo in equipos:
        metrica = metricas.get(equipo.id)
        indicadores = indicadores_por_id[equipo.id]
        estado_actual = estados_por_id.get(equipo.id, {})
        autores = autores_por_equipo.get(equipo.id, [])
        actividad = metrica is not None and metrica.dias_sin_actividad is not None and metrica.dias_sin_actividad >= DIAS_SIN_ACTIVIDAD
        desbalance = (
            metrica is not None
            and indicadores.get("total_commits", 0) > 0
            and indicadores.get("desbalance", 0) / 100 >= UMBRAL_DESBALANCE
        )
        no_descargado = (
            not estado_actual.get("descargado", False)
            or bool(estado_actual.get("error_local"))
            or equipo.estado_salud == "error"
        )
        integrantes_sin_commits = (
            sum(1 for integrante in indicadores.get("integrantes", []) if integrante.get("commits", 0) == 0)
            if metrica is not None
            else 0
        )
        alertas_equipo = {
            "sin_actividad": actividad,
            "desbalance": desbalance,
            "sin_asignar": bool(autores),
            "no_descargado": no_descargado,
            "integrantes_sin_commits": integrantes_sin_commits > 0,
        }
        dominante = max(
            (integrante.get("aporte_pct", 0) for integrante in indicadores.get("integrantes", [])),
            default=0,
        )
        dominante_integrante = max(
            indicadores.get("integrantes", []),
            key=lambda integrante: integrante.get("aporte_pct", 0),
            default=None,
        ) if indicadores.get("total_commits", 0) else None
        semaforo, motivos = evaluar_semaforo(
            indicadores,
            metrica,
            estado_actual,
            equipo.estado_salud,
            len(autores),
        )
        indicadores["integrante_dominante"] = (
            dominante_integrante.get("estudiante") if dominante_integrante else None
        )
        indicadores["integrante_dominante_id"] = (
            dominante_integrante.get("estudiante_id") if dominante_integrante else None
        )
        indicadores["dominante_pct"] = dominante
        indicadores["semaforo"] = semaforo
        indicadores["motivos"] = motivos
        filas.append(
            {
                "equipo": equipo,
                "metrica": metrica,
                "indicadores": indicadores,
                "estado": estado_actual,
                "autores_sin_asignar": len(autores),
                "commits_sin_asignar": sum(autor.commits for autor in autores),
                "commits_ignorados": commits_ignorados_por_equipo.get(equipo.id, 0),
                "integrantes_sin_commits": integrantes_sin_commits,
                "dominante_pct": dominante,
                "integrante_dominante": indicadores["integrante_dominante"],
                "motivos": motivos,
                "alertas": alertas_equipo,
                "semaforo": semaforo,
            }
        )
    return {"filas": filas, "metricas": metricas}


def _sort_filas(filas: list[dict[str, Any]], orden: str, direccion: str) -> list[dict[str, Any]]:
    def valor(fila: dict[str, Any]):
        equipo = fila["equipo"]
        metrica = fila["metrica"]
        return {
            "nombre": equipo.nombre.casefold(),
            "comision": (equipo.comision.cohorte.anio, equipo.comision.numero),
            "commits": metrica.total_commits if metrica else None,
            "ultimo_commit": metrica.ultimo_commit if metrica else None,
            "dias_sin_actividad": metrica.dias_sin_actividad if metrica else None,
            "dominante": fila["dominante_pct"] if metrica else None,
        }[orden]

    validos = [fila for fila in filas if valor(fila) is not None]
    nulos = [fila for fila in filas if valor(fila) is None]
    validos.sort(key=valor, reverse=direccion == "desc")
    return validos + nulos


def _sort_urls(request: Request, orden_actual: str, direccion_actual: str) -> dict[str, str]:
    parametros = dict(request.query_params)
    urls = {}
    for columna in _ORDENES:
        direccion = "desc" if orden_actual == columna and direccion_actual == "asc" else "asc"
        query = {**parametros, "orden": columna, "dir": direccion}
        urls[columna] = f"/equipos?{urlencode(query)}"
    return urls


def _dashboard_context(
    db: Session,
    request: Request,
    cohorte_id: int | None,
    comision_id: int | None,
) -> dict[str, Any]:
    filtros = _filters_context(db, cohorte_id, comision_id)
    cohorte_id = filtros["cohorte_seleccionada"]
    comision_id = filtros["comision_seleccionada"]
    equipos = _equipos_filtrados(db, cohorte_id, comision_id)
    metricas = _ultimas_metricas(db, [equipo.id for equipo in equipos])
    datos = _datos_panel(db, equipos, metricas, cohorte_id, comision_id)
    resumen = resumen_global(db, cohorte=cohorte_id, comision=comision_id)

    estudiantes = (
        db.query(func.count(Estudiante.id))
        .join(Equipo, Estudiante.equipo_id == Equipo.id)
        .join(Comision, Equipo.comision_id == Comision.id)
        .filter(*([Comision.cohorte_id == cohorte_id] if cohorte_id is not None else []))
        .filter(*([Comision.id == comision_id] if comision_id is not None else []))
        .scalar()
        or 0
    )
    comisiones: dict[int, dict[str, Any]] = {}
    conteos_alertas = {nombre: 0 for nombre in _ALERTAS}
    for fila in datos["filas"]:
        equipo = fila["equipo"]
        comision = equipo.comision
        agrupacion = comisiones.setdefault(
            comision.id,
            {
                "comision": comision,
                "equipos": 0,
                "commits": 0,
                "equipos_alerta": set(),
                "sin_asignar": 0,
            },
        )
        agrupacion["equipos"] += 1
        if fila["metrica"] is not None:
            agrupacion["commits"] += fila["metrica"].total_commits
        agrupacion["sin_asignar"] += fila["autores_sin_asignar"]
        for tipo, activa in fila["alertas"].items():
            if activa:
                conteos_alertas[tipo] += 1 if tipo != "integrantes_sin_commits" else fila["integrantes_sin_commits"]
                agrupacion["equipos_alerta"].add(equipo.id)

    alertas = [
        {"tipo": "sin_actividad", "titulo": "Sin actividad", "cantidad": conteos_alertas["sin_actividad"], "href": "sin_actividad"},
        {"tipo": "desbalance", "titulo": "Desbalance", "cantidad": conteos_alertas["desbalance"], "href": "desbalance"},
        {
            "tipo": "sin_asignar",
            "titulo": "Autores sin asignar",
            "cantidad": resumen["totales"]["autores_sin_asignar"],
            "detalle": f"{resumen['totales']['commits_sin_asignar']} commits afectados",
            "href": "sin_asignar",
        },
        {"tipo": "no_descargado", "titulo": "No descargado o con error", "cantidad": conteos_alertas["no_descargado"], "href": "no_descargado"},
        {"tipo": "integrantes_sin_commits", "titulo": "Integrantes sin commits", "cantidad": conteos_alertas["integrantes_sin_commits"], "href": "integrantes_sin_commits"},
    ]
    comisiones_ordenadas = sorted(
        comisiones.values(),
        key=lambda item: (item["comision"].cohorte.anio, item["comision"].numero),
        reverse=True,
    )
    for grupo in comisiones_ordenadas:
        grupo["equipos_alerta"] = len(grupo["equipos_alerta"])

    return {
        "request": request,
        **filtros,
        "resumen": resumen,
        "estudiantes_total": estudiantes,
        "repos_descargados": sum(bool(fila["estado"].get("descargado")) for fila in datos["filas"]),
        "comisiones_resumen": comisiones_ordenadas,
        "alertas_panel": alertas,
        "progreso": estado(),
    }


@router.get("/", name="dashboard")
def dashboard(
    request: Request,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    db: Session = Depends(get_db),
):
    cohorte_id = _parse_id(cohorte_id, "cohorte")
    comision_id = _parse_id(comision_id, "comisión")
    contexto = _dashboard_context(db, request, cohorte_id, comision_id)
    return templates.TemplateResponse(request=request, name="dashboard.html", context=contexto)


def _equipos_context(
    db: Session,
    request: Request,
    cohorte_id: int | None,
    comision_id: int | None,
    alerta: str | None,
    orden: str,
    direccion: str,
) -> dict[str, Any]:
    filtros = _filters_context(db, cohorte_id, comision_id)
    cohorte_id = filtros["cohorte_seleccionada"]
    comision_id = filtros["comision_seleccionada"]
    equipos = _equipos_filtrados(db, cohorte_id, comision_id)
    metricas = _ultimas_metricas(db, [equipo.id for equipo in equipos])
    datos = _datos_panel(db, equipos, metricas, cohorte_id, comision_id)
    filas = datos["filas"]
    if alerta:
        filas = [fila for fila in filas if fila["alertas"].get(alerta, False)]
    filas = _sort_filas(filas, orden, direccion)
    return {
        "request": request,
        **filtros,
        "filas": filas,
        "alerta": alerta,
        "orden": orden,
        "direccion": direccion,
        "sort_urls": _sort_urls(request, orden, direccion),
        "progress_url": f"/equipos/progreso?{urlencode({key: value for key, value in {'cohorte_id': filtros['cohorte_seleccionada'], 'comision_id': filtros['comision_seleccionada'], 'alerta': alerta, 'orden': orden, 'dir': direccion}.items() if value is not None and value != ''})}",
        "recalcular_url": f"/equipos/recalcular?{urlencode({key: value for key, value in {'cohorte_id': filtros['cohorte_seleccionada'], 'comision_id': filtros['comision_seleccionada'], 'alerta': alerta, 'orden': orden, 'dir': direccion}.items() if value is not None and value != ''})}",
        "progreso": estado(),
    }


@router.get("/equipos", name="lista_equipos")
def lista_equipos(
    request: Request,
    alerta: str | None = None,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    orden: str = "nombre",
    dir: str = "asc",
    db: Session = Depends(get_db),
):
    if alerta and alerta not in _ALERTAS:
        raise HTTPException(status_code=422, detail="El filtro alerta no es válido")
    if orden not in _ORDENES:
        raise HTTPException(status_code=422, detail="La columna de orden no es válida")
    if dir not in {"asc", "desc"}:
        raise HTTPException(status_code=422, detail="La dirección debe ser asc o desc")
    contexto = _equipos_context(
        db,
        request,
        _parse_id(cohorte_id, "cohorte"),
        _parse_id(comision_id, "comisión"),
        alerta,
        orden,
        dir,
    )
    return templates.TemplateResponse(request=request, name="equipos.html", context={**contexto, "vista": "pagina"})


@router.get("/equipos/nuevo", name="nuevo_equipo")
def nuevo_equipo(request: Request, db: Session = Depends(get_db)):
    contexto = _equipo_form_context(db, request)
    return templates.TemplateResponse(request=request, name="equipo_form.html", context=contexto)


@router.get("/equipos/opciones-comision", name="opciones_comision")
def opciones_comision(
    request: Request,
    cohorte_id: int | None = None,
    db: Session = Depends(get_db),
):
    if cohorte_id is not None and cohorte_id < 1:
        raise HTTPException(status_code=422, detail="La cohorte debe ser positiva")
    query = db.query(Comision).options(joinedload(Comision.cohorte))
    if cohorte_id is not None:
        query = query.filter(Comision.cohorte_id == cohorte_id)
    comisiones = query.join(Cohorte).order_by(Cohorte.anio.desc(), Comision.numero).all()
    return templates.TemplateResponse(
        request=request,
        name="_comisiones_opciones.html",
        context={"comisiones": comisiones, "comision_seleccionada": None},
    )


@router.post("/equipos", name="crear_equipo")
def crear_equipo_route(
    request: Request,
    cohorte_id: str = Form(""),
    comision_id: str = Form(""),
    nombre: str = Form(""),
    proyecto_nombre: str = Form(""),
    url_github: str = Form(""),
    db: Session = Depends(get_db),
):
    cohorte, error = _form_id(cohorte_id, "una cohorte")
    comision, error_comision = _form_id(comision_id, "una comisión")
    error = error or error_comision
    valores = {
        "nombre": nombre,
        "proyecto_nombre": proyecto_nombre,
        "url_github": url_github,
    }
    if error is None:
        resultado = servicio_crear_equipo(
            db,
            cohorte,
            comision,
            nombre,
            url_github,
            proyecto_nombre or None,
        )
        if resultado.get("ok"):
            query = urlencode({"aviso": resultado.get("mensaje", "Equipo creado correctamente."), "equipo_nuevo": "1"})
            return RedirectResponse(f"/equipos/{resultado['equipo_id']}?{query}", status_code=303)
        error = resultado.get("mensaje", "No se pudo crear el equipo.")
    contexto = _equipo_form_context(
        db, request, valores=valores, error=error, cohorte_id=cohorte, comision_id=comision
    )
    return templates.TemplateResponse(
        request=request,
        name="equipo_form.html",
        context=contexto,
        status_code=422 if error else 200,
    )


@router.get("/equipos/{equipo_id}/editar", name="editar_equipo_form")
def editar_equipo_form(equipo_id: int, request: Request, db: Session = Depends(get_db)):
    equipo = (
        db.query(Equipo)
        .options(joinedload(Equipo.comision).joinedload(Comision.cohorte))
        .filter(Equipo.id == equipo_id)
        .first()
    )
    if equipo is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    contexto = _equipo_form_context(db, request, equipo=equipo)
    return templates.TemplateResponse(request=request, name="equipo_form.html", context=contexto)


@router.post("/equipos/{equipo_id}/editar", name="editar_equipo_route")
def editar_equipo_route(
    equipo_id: int,
    request: Request,
    cohorte_id: str = Form(""),
    comision_id: str = Form(""),
    nombre: str = Form(""),
    proyecto_nombre: str = Form(""),
    url_github: str = Form(""),
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
    cohorte, error = _form_id(cohorte_id, "una cohorte")
    comision, error_comision = _form_id(comision_id, "una comisión")
    error = error or error_comision
    if error is None:
        pertenece = db.query(Comision.id).filter(
            Comision.id == comision, Comision.cohorte_id == cohorte
        ).first()
        if pertenece is None:
            error = "La comisión no pertenece a la cohorte seleccionada."
    valores = {"nombre": nombre, "proyecto_nombre": proyecto_nombre, "url_github": url_github}
    if error is None:
        resultado = servicio_editar_equipo(
            db,
            equipo_id,
            nombre=nombre,
            url_github=url_github,
            proyecto_nombre=proyecto_nombre,
            comision_id=comision,
        )
        if resultado.get("ok"):
            params = {
                "aviso": (
                    "La copia local se eliminó; descargá de nuevo"
                    if resultado.get("requiere_descarga")
                    else resultado.get("mensaje", "Equipo actualizado correctamente.")
                )
            }
            if resultado.get("requiere_descarga"):
                params["requiere_descarga"] = "1"
            return RedirectResponse(f"/equipos/{equipo_id}?{urlencode(params)}", status_code=303)
        error = resultado.get("mensaje", "No se pudo editar el equipo.")
    contexto = _equipo_form_context(
        db,
        request,
        equipo=equipo,
        valores=valores,
        error=error,
        cohorte_id=cohorte,
        comision_id=comision,
    )
    return templates.TemplateResponse(
        request=request,
        name="equipo_form.html",
        context=contexto,
        status_code=422 if error else 200,
    )


@router.post("/equipos/{equipo_id}/integrantes", name="agregar_integrante_route")
def agregar_integrante_route(
    equipo_id: int,
    request: Request,
    apellido_nombre: str = Form(""),
    dni: str = Form(""),
    email: str = Form(""),
    usuario_github: str = Form(""),
    db: Session = Depends(get_db),
):
    if db.get(Equipo, equipo_id) is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    resultado = servicio_agregar_integrante(
        db, equipo_id, apellido_nombre, dni, email or None, usuario_github or None
    )
    if resultado.get("ok"):
        params = {
            "requiere_calculo": "1" if resultado.get("requiere_calculo") else "0",
            "aviso": resultado.get("mensaje", "Integrante agregado correctamente."),
        }
        return RedirectResponse(f"/equipos/{equipo_id}?{urlencode(params)}", status_code=303)
    params = {"integrante_error": resultado.get("mensaje", "No se pudo agregar el integrante.")}
    conflicto = resultado.get("conflicto")
    if conflicto:
        params["conflicto_id"] = str(conflicto["estudiante_id"])
        params["conflicto_equipo"] = conflicto.get("equipo") or "otro equipo"
    return RedirectResponse(f"/equipos/{equipo_id}?{urlencode(params)}", status_code=303)


@router.post(
    "/equipos/{equipo_id}/integrantes/{estudiante_id}/mover",
    name="mover_integrante_route",
)
def mover_integrante_route(
    equipo_id: int,
    estudiante_id: int,
    db: Session = Depends(get_db),
):
    resultado = servicio_mover_integrante(db, estudiante_id, equipo_id)
    if not resultado.get("ok"):
        return RedirectResponse(
            f"/equipos/{equipo_id}?{urlencode({'integrante_error': resultado.get('mensaje', 'No se pudo mover el integrante.')})}",
            status_code=303,
        )
    params = {
        "aviso": resultado.get("mensaje", "Integrante trasladado correctamente."),
        "requiere_calculo": "1" if resultado.get("requiere_calculo") else "0",
    }
    return RedirectResponse(f"/equipos/{equipo_id}?{urlencode(params)}", status_code=303)


@router.post("/equipos/{equipo_id}/recalcular", name="recalcular_equipo")
def recalcular_equipo(equipo_id: int, request: Request, db: Session = Depends(get_db)):
    equipo = (
        db.query(Equipo)
        .options(joinedload(Equipo.comision).joinedload(Comision.cohorte), joinedload(Equipo.estudiantes))
        .filter(Equipo.id == equipo_id)
        .first()
    )
    if equipo is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    iniciado = ejecutar("calcular", equipo_id=equipo_id)
    fila = _datos_panel(db, [equipo], cohorte_id=equipo.comision.cohorte_id, comision_id=equipo.comision_id)["filas"][0]
    fila["mensaje"] = "Recalculo iniciado." if iniciado else "Hay un proceso en curso."
    fila["proceso_activo"] = estado().get("running", False)
    return templates.TemplateResponse(
        request=request,
        name="_equipos_fila.html",
        context={"fila": fila, "proceso_activo": fila["proceso_activo"]},
    )


def _progreso_response(request: Request, db: Session, progreso: dict[str, Any]):
    cohorte_id = progreso.get("cohorte")
    comision_id = progreso.get("comision")
    alerta = request.query_params.get("alerta") or None
    orden = request.query_params.get("orden", "nombre")
    direccion = request.query_params.get("dir", "asc")
    contexto = _equipos_context(db, request, cohorte_id, comision_id, alerta, orden, direccion)
    if progreso.get("mensaje"):
        contexto["mensaje"] = progreso["mensaje"]
    return templates.TemplateResponse(
        request=request,
        name="equipos.html",
        context={**contexto, "vista": "progreso", "progreso": progreso},
    )


@router.post("/equipos/recalcular", name="recalcular_todos")
def recalcular_todos(
    request: Request,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    alerta: str | None = None,
    orden: str = "nombre",
    dir: str = "asc",
    db: Session = Depends(get_db),
):
    if alerta and alerta not in _ALERTAS:
        raise HTTPException(status_code=422, detail="El filtro alerta no es válido")
    if orden not in _ORDENES or dir not in {"asc", "desc"}:
        orden, dir = "nombre", "asc"
    cohorte_id = _parse_id(cohorte_id, "cohorte")
    comision_id = _parse_id(comision_id, "comisión")
    iniciado = ejecutar("calcular", cohorte_id=cohorte_id, comision_id=comision_id)
    progreso = estado()
    if not iniciado:
        progreso["mensaje"] = "Hay un proceso en curso."
    elif not progreso.get("total"):
        progreso["mensaje"] = "No hay equipos que coincidan con los filtros."
    else:
        progreso["mensaje"] = "Recalculo iniciado para los equipos filtrados."
    return _progreso_response(request, db, progreso)


@router.get("/equipos/progreso", name="progreso_equipos")
def progreso_equipos(request: Request, db: Session = Depends(get_db)):
    return _progreso_response(request, db, estado())


def _render_equipo_acciones(
    request: Request,
    equipo_id: int,
    progreso: dict[str, Any],
    mensaje: str | None = None,
):
    return templates.TemplateResponse(
        request=request,
        name="_equipo_acciones.html",
        context={
            "request": request,
            "equipo_id": equipo_id,
            "progreso": progreso,
            "proceso_activo": bool(progreso.get("running")),
            "mensaje": mensaje,
        },
    )


@router.post("/equipos/{equipo_id}/accion", name="accion_equipo")
def accion_equipo(
    equipo_id: int,
    request: Request,
    accion: str = Form(...),
    db: Session = Depends(get_db),
):
    if db.get(Equipo, equipo_id) is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    if accion not in {"descargar", "calcular", "sincronizar_calcular"}:
        raise HTTPException(status_code=422, detail="La acción no es válida")
    iniciado = ejecutar(accion, equipo_id=equipo_id)
    progreso = estado()
    mensaje = (
        "Proceso iniciado."
        if iniciado
        else "Hay un lote activo; las acciones están deshabilitadas hasta que termine."
    )
    return _render_equipo_acciones(request, equipo_id, progreso, mensaje)


@router.get("/equipos/{equipo_id}/acciones", name="estado_acciones_equipo")
def estado_acciones_equipo(equipo_id: int, request: Request, db: Session = Depends(get_db)):
    if db.get(Equipo, equipo_id) is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    return _render_equipo_acciones(request, equipo_id, estado())


@router.get("/equipos/{equipo_id}", name="detalle_equipo")
def detalle_equipo(equipo_id: int, request: Request, db: Session = Depends(get_db)):
    equipo = (
        db.query(Equipo)
        .options(
            joinedload(Equipo.comision).joinedload(Comision.cohorte),
            joinedload(Equipo.estudiantes),
        )
        .filter(Equipo.id == equipo_id)
        .first()
    )
    if equipo is None:
        raise HTTPException(status_code=404, detail="Equipo no encontrado")
    metrica = (
        db.query(MetricaProyecto)
        .filter(MetricaProyecto.equipo_id == equipo.id)
        .order_by(MetricaProyecto.fecha.desc(), MetricaProyecto.id.desc())
        .first()
    )
    historial = (
        db.query(MetricaProyecto)
        .filter(MetricaProyecto.equipo_id == equipo.id)
        .order_by(MetricaProyecto.fecha.desc(), MetricaProyecto.id.desc())
        .limit(10)
        .all()
    )
    autores = (
        db.query(AutorGit)
        .options(joinedload(AutorGit.equipo).joinedload(Equipo.estudiantes))
        .filter(
            AutorGit.equipo_id == equipo.id,
            AutorGit.estudiante_id.is_(None),
            AutorGit.ignorado.is_(False),
            AutorGit.commits > 0,
        )
        .order_by(AutorGit.commits.desc(), AutorGit.nombre_git)
        .all()
    )
    autores_ignorados = (
        db.query(AutorGit)
        .filter(AutorGit.equipo_id == equipo.id, AutorGit.ignorado.is_(True))
        .order_by(AutorGit.commits.desc(), AutorGit.nombre_git, AutorGit.email)
        .all()
    )
    autores_estado = db.query(AutorGit).filter(AutorGit.equipo_id == equipo.id).all()
    commits_integrantes = sum(
        autor.commits
        for autor in autores_estado
        if autor.estudiante_id is not None and not autor.ignorado
    )
    commits_ignorados = sum(autor.commits for autor in autores_estado if autor.ignorado)
    commits_sin_asignar = sum(
        autor.commits
        for autor in autores_estado
        if autor.estudiante_id is None and not autor.ignorado
    )
    total_commits_desglose = (
        metrica.total_commits
        if metrica is not None
        else commits_integrantes + commits_ignorados + commits_sin_asignar
    )
    estado_actual = next(
        (
            fila["estado"]
            for fila in estado_equipos(
                db,
                cohorte_id=equipo.comision.cohorte_id,
                comision_id=equipo.comision_id,
            )
            if fila["equipo"].id == equipo.id
        ),
        {},
    )
    indicadores = indicadores_equipo(
        db,
        equipo.id,
        estado_repo=estado_actual,
        autores_sin_asignar=len(autores),
    )
    estudiantes_ids = [estudiante.id for estudiante in equipo.estudiantes]
    metricas_estudiantes: dict[int, MetricaEstudiante] = {}
    if estudiantes_ids:
        filas_estudiantes = (
            db.query(MetricaEstudiante)
            .filter(MetricaEstudiante.estudiante_id.in_(estudiantes_ids))
            .order_by(MetricaEstudiante.fecha.desc(), MetricaEstudiante.id.desc())
            .all()
        )
        for fila in filas_estudiantes:
            metricas_estudiantes.setdefault(fila.estudiante_id, fila)

    semaforo = indicadores["semaforo"]
    evaluacion = evaluar_equipo(db, equipo.id)

    detalle = _detalle_metrica(metrica)
    serie_semanal = _serie_metrica(detalle, "commits_por_semana")
    return templates.TemplateResponse(
        request=request,
        name="equipo_detail.html",
        context={
            "request": request,
            "equipo": equipo,
            "metrica": metrica,
            "historial": historial,
            "metricas_estudiantes": metricas_estudiantes,
            "indicadores": indicadores,
            "semaforo": semaforo,
            "evaluacion": evaluacion if evaluacion.get("ok", True) else None,
            "dias_sin_actividad_umbral": DIAS_SIN_ACTIVIDAD,
            "desbalance_umbral_pct": UMBRAL_DESBALANCE * 100,
            "estado_repo": estado_actual,
            "serie_semanal": serie_semanal,
            "autores": autores,
            "autores_ignorados": autores_ignorados,
            "commits_integrantes": commits_integrantes,
            "commits_ignorados": commits_ignorados,
            "commits_sin_asignar": commits_sin_asignar,
            "total_commits_desglose": total_commits_desglose,
            "equipo_id": equipo.id,
            "pestana": "pendientes",
            "equipos_calculados": {equipo.id} if metrica is not None else set(),
            "filtros": {"cohorte_id": None, "comision_id": equipo.comision_id, "equipo_id": equipo.id},
            "progreso": estado(),
            "aviso": request.query_params.get("aviso"),
            "aviso_tipo": (
                "warning"
                if request.query_params.get("requiere_descarga") == "1"
                or request.query_params.get("requiere_calculo") == "1"
                else "success"
            ),
            "requiere_descarga": request.query_params.get("requiere_descarga") == "1",
            "requiere_calculo": request.query_params.get("requiere_calculo") == "1",
            "equipo_nuevo": request.query_params.get("equipo_nuevo") == "1",
            "integrante_error": request.query_params.get("integrante_error"),
            "integrante_conflicto": (
                {
                    "estudiante_id": int(request.query_params["conflicto_id"]),
                    "equipo": request.query_params.get("conflicto_equipo"),
                }
                if request.query_params.get("conflicto_id", "").isdigit()
                else None
            ),
            "integrante_valores": {},
            "proceso_activo": bool(estado().get("running")),
        },
    )


@router.get("/estudiantes/{estudiante_id}", name="detalle_estudiante")
def detalle_estudiante(estudiante_id: int, request: Request, db: Session = Depends(get_db)):
    estudiante = (
        db.query(Estudiante)
        .options(
            joinedload(Estudiante.equipo)
            .joinedload(Equipo.comision)
            .joinedload(Comision.cohorte)
        )
        .filter(Estudiante.id == estudiante_id)
        .first()
    )
    if estudiante is None:
        raise HTTPException(status_code=404, detail="Estudiante no encontrado")
    metrica = (
        db.query(MetricaEstudiante)
        .filter(MetricaEstudiante.estudiante_id == estudiante.id)
        .order_by(MetricaEstudiante.fecha.desc(), MetricaEstudiante.id.desc())
        .first()
    )
    detalle = _detalle_metrica(metrica)
    autores_asignados = (
        db.query(AutorGit)
        .filter(AutorGit.estudiante_id == estudiante.id)
        .order_by(AutorGit.email)
        .all()
    )
    progreso = estado()
    return templates.TemplateResponse(
        request=request,
        name="estudiante_detail.html",
        context={
            "request": request,
            "estudiante": estudiante,
            "equipo": estudiante.equipo,
            "metrica": metrica,
            "serie_semanal": _serie_metrica(detalle, "commits_por_semana"),
            "serie_horas": _serie_metrica(detalle, "commits_por_hora", horas=True),
            "autores_asignados": autores_asignados,
            "equipo_id": estudiante.equipo_id,
            "progreso": progreso,
            "proceso_activo": bool(progreso.get("running")),
        },
    )