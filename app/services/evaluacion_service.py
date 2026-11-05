"""Cálculo y persistencia de las evaluaciones por rúbrica."""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Mapping

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models import (
    Cohorte,
    Comision,
    ConfigEvaluacion,
    CriterioEvaluacion,
    Equipo,
    Evaluacion,
    Rubrica,
    Valoracion,
)


_CERO = Decimal("0")
_CIEN = Decimal("100")
_DOS_DECIMALES = Decimal("0.01")


def _get(obj: Any, clave: str, defecto: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(clave, defecto)
    return getattr(obj, clave, defecto)


def _decimal(valor: Any, defecto: Decimal = _CERO) -> Decimal:
    if valor is None:
        return defecto
    try:
        resultado = Decimal(str(valor))
        return resultado if resultado.is_finite() else defecto
    except (InvalidOperation, TypeError, ValueError):
        return defecto


def _mostrar(valor: Decimal | None) -> float | None:
    if valor is None:
        return None
    return float(valor.quantize(_DOS_DECIMALES, rounding=ROUND_HALF_UP))


def _config_dict(config: Any) -> dict[str, Any]:
    if isinstance(config, Mapping):
        return dict(config)
    resultado: dict[str, Any] = {}
    if config is None:
        return resultado
    for elemento in config:
        clave = _get(elemento, "clave")
        if clave is not None:
            resultado[str(clave)] = _get(elemento, "valor")
    return resultado


def _valoraciones_por_criterio(valoraciones: Any) -> dict[Any, Any]:
    if valoraciones is None:
        return {}
    if isinstance(valoraciones, Mapping):
        return dict(valoraciones)
    return {
        _get(valoracion, "criterio_id"): valoracion
        for valoracion in valoraciones
        if _get(valoracion, "criterio_id") is not None
    }


def calcular(criterios: Any, valoraciones: Any, config: Any) -> dict[str, Any]:
    """Calcula puntajes y estado sin modificar los argumentos recibidos.

    ``criterios`` y ``valoraciones`` admiten objetos ORM o diccionarios, lo que
    permite probar esta función sin una sesión ni una base de datos.
    """
    configuracion = _config_dict(config)
    umbral_total = _decimal(configuracion.get("umbral_total_pct", 60), Decimal("60"))
    umbral_rubrica = _decimal(configuracion.get("umbral_rubrica_pct", 60), Decimal("60"))
    escala = _decimal(configuracion.get("escala_max", 5), Decimal("5"))
    if escala <= 0:
        escala = Decimal("5")

    valoraciones_idx = _valoraciones_por_criterio(valoraciones)
    rubricas: dict[Any, dict[str, Any]] = {}
    detalle_criterios: list[dict[str, Any]] = []
    total = _CERO
    maximo_total = _CERO
    criterios_evaluables = 0
    criterios_valorados = 0

    for criterio in criterios:
        criterio_id = _get(criterio, "id")
        rubrica_obj = _get(criterio, "rubrica")
        rubrica_id = _get(criterio, "rubrica_id", _get(rubrica_obj, "id"))
        rubrica_codigo = _get(criterio, "rubrica_codigo", _get(rubrica_obj, "codigo"))
        rubrica_nombre = _get(criterio, "rubrica_nombre", _get(rubrica_obj, "nombre"))
        if rubrica_id not in rubricas:
            rubricas[rubrica_id] = {
                "rubrica_id": rubrica_id,
                "codigo": rubrica_codigo,
                "nombre": rubrica_nombre,
                "puntaje_decimal": _CERO,
                "maximo_decimal": _CERO,
                "completa": True,
                "criterios": [],
            }
        rubrica = rubricas[rubrica_id]

        peso = _decimal(_get(criterio, "peso"))
        activo = bool(_get(criterio, "activo", True))
        cuenta = activo and peso > 0
        valoracion = valoraciones_idx.get(criterio_id)
        if isinstance(valoracion, (int, Decimal)) and not isinstance(valoracion, bool):
            nivel_raw = valoracion
        else:
            nivel_raw = _get(valoracion, "nivel") if valoracion is not None else None
        try:
            nivel = int(nivel_raw) if nivel_raw is not None else None
        except (TypeError, ValueError, OverflowError):
            nivel = None
        if nivel is not None and not 0 <= nivel <= 5:
            nivel = None

        puntaje_decimal = peso * Decimal(nivel) / escala if cuenta and nivel is not None else _CERO
        if cuenta:
            criterios_evaluables += 1
            maximo_total += peso
            rubrica["maximo_decimal"] += peso
            if nivel is None:
                rubrica["completa"] = False
            else:
                criterios_valorados += 1
                total += puntaje_decimal
                rubrica["puntaje_decimal"] += puntaje_decimal

        detalle = {
            "criterio_id": criterio_id,
            "codigo": _get(criterio, "codigo"),
            "nombre": _get(criterio, "nombre"),
            "rubrica_id": rubrica_id,
            "nivel": nivel,
            "puntaje": _mostrar(puntaje_decimal) if nivel is not None and cuenta else (0.0 if cuenta else None),
            "observacion": (
                _get(valoracion, "observacion")
                if valoracion is not None and not isinstance(valoracion, (int, Decimal))
                else None
            ),
            "activo": activo,
            "peso": _mostrar(peso),
            "cuenta": cuenta,
        }
        detalle_criterios.append(detalle)
        rubrica["criterios"].append(detalle)

    rubricas_resultado: list[dict[str, Any]] = []
    rubricas_evaluables: list[dict[str, Any]] = []
    for rubrica in rubricas.values():
        maximo = rubrica.pop("maximo_decimal")
        puntaje = rubrica.pop("puntaje_decimal")
        pct = puntaje * _CIEN / maximo if maximo > 0 else None
        resultado = {
            **rubrica,
            "puntaje": _mostrar(puntaje),
            "maximo": _mostrar(maximo),
            "pct": _mostrar(pct),
        }
        rubricas_resultado.append(resultado)
        if maximo > 0:
            rubricas_evaluables.append({"pct": pct, "codigo": resultado["codigo"]})

    pct_total = total * _CIEN / maximo_total if maximo_total > 0 else None
    motivos: list[str] = []
    if criterios_evaluables == 0 or criterios_valorados == 0:
        estado = "sin_evaluar"
        motivos.append("No hay criterios evaluables valorados.")
    elif criterios_valorados < criterios_evaluables:
        estado = "parcial"
        motivos.append(f"Faltan {criterios_evaluables - criterios_valorados} criterio(s) por valorar.")
    else:
        no_aprueba_total = pct_total is None or pct_total < umbral_total
        rubricas_bajo_umbral = [
            rubrica for rubrica in rubricas_evaluables
            if rubrica["pct"] is not None and rubrica["pct"] < umbral_rubrica
        ]
        if no_aprueba_total or rubricas_bajo_umbral:
            estado = "no_aprobado"
            if no_aprueba_total:
                motivos.append(f"El total no alcanza el { _mostrar(umbral_total) }% requerido.")
            if rubricas_bajo_umbral:
                codigos = ", ".join(str(r["codigo"] or "rúbrica") for r in rubricas_bajo_umbral)
                motivos.append(f"No alcanzan el umbral las rúbricas: {codigos}.")
        else:
            estado = "aprobado"
            motivos.append("Alcanza los umbrales total y por rúbrica.")

    return {
        "rubricas": rubricas_resultado,
        "criterios": detalle_criterios,
        "total": _mostrar(total),
        "maximo_total": _mostrar(maximo_total),
        "pct_total": _mostrar(pct_total),
        "estado": estado,
        "motivos": motivos,
    }


def _criterios(db: Session) -> list[CriterioEvaluacion]:
    return (
        db.query(CriterioEvaluacion)
        .options(joinedload(CriterioEvaluacion.rubrica))
        .join(Rubrica)
        .order_by(Rubrica.orden, CriterioEvaluacion.orden, CriterioEvaluacion.id)
        .all()
    )


def _config(db: Session) -> dict[str, str]:
    return {
        fila.clave: fila.valor
        for fila in db.query(ConfigEvaluacion).all()
    }


def _equipos_query(
    db: Session, cohorte_id: int | None = None, comision_id: int | None = None,
):
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
    return consulta.order_by(Cohorte.anio.desc(), Comision.numero, Equipo.nombre, Equipo.id)


def _evaluar_lote(
    db: Session,
    equipos: list[Equipo],
    criterios: list[CriterioEvaluacion] | None = None,
    config: dict[str, Any] | None = None,
    pesos: Mapping[Any, Any] | None = None,
) -> list[dict[str, Any]]:
    if criterios is None:
        criterios = _criterios(db)
    if config is None:
        config = _config(db)
    equipo_ids = [equipo.id for equipo in equipos]
    evaluaciones = (
        db.query(Evaluacion).filter(Evaluacion.equipo_id.in_(equipo_ids)).all()
        if equipo_ids else []
    )
    eval_por_equipo = {evaluacion.equipo_id: evaluacion for evaluacion in evaluaciones}
    eval_ids = [evaluacion.id for evaluacion in evaluaciones]
    valoraciones = (
        db.query(Valoracion).filter(Valoracion.evaluacion_id.in_(eval_ids)).all()
        if eval_ids else []
    )
    valores_por_eval: dict[int, dict[int, Valoracion]] = defaultdict(dict)
    for valoracion in valoraciones:
        valores_por_eval[valoracion.evaluacion_id][valoracion.criterio_id] = valoracion

    criterios_por_equipo: dict[int, dict[str, Any]] = {}
    for equipo in equipos:
        eval_actual = eval_por_equipo.get(equipo.id)
        criterios_calculo: list[Any] = criterios
        if pesos:
            criterios_calculo = [
                {
                    "id": criterio.id,
                    "rubrica_id": criterio.rubrica_id,
                    "rubrica_codigo": criterio.rubrica.codigo,
                    "rubrica_nombre": criterio.rubrica.nombre,
                    "codigo": criterio.codigo,
                    "nombre": criterio.nombre,
                    "peso": pesos.get(criterio.id, criterio.peso),
                    "activo": criterio.activo,
                }
                for criterio in criterios
            ]
        calculo = calcular(
            criterios_calculo,
            valores_por_eval.get(eval_actual.id, {}) if eval_actual else {},
            config,
        )
        comision = equipo.comision
        cohorte = comision.cohorte
        criterios_por_equipo[equipo.id] = {
            "equipo_id": equipo.id,
            "nombre": equipo.nombre,
            "nombre_proyecto": equipo.nombre_proyecto,
            "url_github": equipo.url_github,
            "comision_id": comision.id,
            "comision": comision.numero,
            "cohorte_id": cohorte.id,
            "cohorte": cohorte.anio,
            **calculo,
        }
    return list(criterios_por_equipo.values())


def evaluar_equipo(db: Session, equipo_id: int) -> dict[str, Any]:
    equipo = _equipos_query(db).filter(Equipo.id == equipo_id).first()
    if equipo is None:
        return {"ok": False, "mensaje": "Equipo no encontrado."}
    return _evaluar_lote(db, [equipo])[0]


def evaluar_todos(
    db: Session, cohorte_id: int | None = None, comision_id: int | None = None,
) -> list[dict[str, Any]]:
    """Evalúa en lote: carga equipos, criterios, evaluaciones y valoraciones."""
    equipos = _equipos_query(db, cohorte_id, comision_id).all()
    return _evaluar_lote(db, equipos)


def listar_filtros(
    db: Session, cohorte_id: int | None = None,
) -> dict[str, Any]:
    """Devuelve cohortes y comisiones para los filtros de evaluación."""
    cohortes = db.query(Cohorte).order_by(Cohorte.anio.desc()).all()
    consulta = db.query(Comision).join(Cohorte)
    if cohorte_id is not None:
        consulta = consulta.filter(Comision.cohorte_id == cohorte_id)
    comisiones = consulta.order_by(Cohorte.anio.desc(), Comision.numero).all()
    return {
        "cohortes": [{"id": item.id, "anio": item.anio} for item in cohortes],
        "comisiones": [
            {
                "id": item.id,
                "numero": item.numero,
                "cohorte_id": item.cohorte_id,
            }
            for item in comisiones
        ],
    }


def obtener_umbrales(db: Session) -> dict[str, float]:
    """Lee la configuración de umbrales con valores predeterminados seguros."""
    configuracion = _config(db)
    return {
        "total_pct": float(_decimal(configuracion.get("umbral_total_pct"), Decimal("60"))),
        "rubrica_pct": float(_decimal(configuracion.get("umbral_rubrica_pct"), Decimal("60"))),
    }


def valorar(
    db: Session,
    equipo_id: int,
    criterio_id: int,
    nivel: int | None,
    observacion: str | None = None,
    evaluador: str | None = None,
) -> dict[str, Any]:
    if nivel is not None and (isinstance(nivel, bool) or not isinstance(nivel, int) or not 0 <= nivel <= 5):
        return {"ok": False, "mensaje": "El nivel debe ser un entero entre 0 y 5, o None para borrar."}
    equipo = db.query(Equipo).filter(Equipo.id == equipo_id).first()
    if equipo is None:
        return {"ok": False, "mensaje": "Equipo no encontrado."}
    criterio = db.query(CriterioEvaluacion).filter(CriterioEvaluacion.id == criterio_id).first()
    if criterio is None:
        return {"ok": False, "mensaje": "Criterio no encontrado."}
    evaluacion = db.query(Evaluacion).filter(Evaluacion.equipo_id == equipo_id).first()
    try:
        if evaluacion is None and nivel is not None:
            evaluacion = Evaluacion(
                equipo_id=equipo_id,
                evaluador=evaluador or "Sin especificar",
            )
            db.add(evaluacion)
            db.flush()
        if evaluacion is not None:
            valoracion_actual = (
                db.query(Valoracion)
                .filter(
                    Valoracion.evaluacion_id == evaluacion.id,
                    Valoracion.criterio_id == criterio_id,
                )
                .first()
            )
            if nivel is None:
                if valoracion_actual is not None:
                    db.delete(valoracion_actual)
            elif valoracion_actual is None:
                db.add(Valoracion(
                    evaluacion_id=evaluacion.id,
                    criterio_id=criterio_id,
                    nivel=nivel,
                    observacion=observacion,
                ))
            else:
                valoracion_actual.nivel = nivel
                valoracion_actual.observacion = observacion
            if evaluador and evaluacion.evaluador != evaluador:
                evaluacion.evaluador = evaluador
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo guardar la valoración: {exc}"}
    resultado = evaluar_equipo(db, equipo_id)
    return {"ok": True, **resultado}


def listar_rubricas(db: Session) -> list[dict[str, Any]]:
    rubricas = db.query(Rubrica).order_by(Rubrica.orden, Rubrica.id).all()
    criterios = _criterios(db)
    por_rubrica: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for criterio in criterios:
        por_rubrica[criterio.rubrica_id].append({
            "id": criterio.id,
            "codigo": criterio.codigo,
            "nombre": criterio.nombre,
            "peso": criterio.peso,
            "orden": criterio.orden,
            "activo": criterio.activo,
            "descripcion": criterio.descripcion,
            "que_revisar": criterio.que_revisar,
            "evidencia_adecuado": criterio.evidencia_adecuado,
            "evidencia_solido": criterio.evidencia_solido,
            "evidencia_destacado": criterio.evidencia_destacado,
            "ejemplo_observacion": criterio.ejemplo_observacion,
        })
    return [
        {
            "id": rubrica.id,
            "codigo": rubrica.codigo,
            "nombre": rubrica.nombre,
            "orden": rubrica.orden,
            "guia": rubrica.guia,
            "criterios": por_rubrica.get(rubrica.id, []),
        }
        for rubrica in rubricas
    ]


def impacto_pesos(db: Session, pesos_propuestos: Mapping[Any, Any]) -> dict[str, Any]:
    """Compara estados actuales y simulados sin persistir los pesos."""
    pesos: dict[int, Decimal] = {}
    try:
        for criterio_id, peso in pesos_propuestos.items():
            peso_decimal = _decimal(peso, Decimal("NaN"))
            if not peso_decimal.is_finite() or peso_decimal < 0:
                return {"ok": False, "mensaje": "Los pesos deben ser números mayores o iguales a cero."}
            pesos[int(criterio_id)] = peso_decimal
    except (TypeError, ValueError, OverflowError):
        return {"ok": False, "mensaje": "Los identificadores y pesos propuestos no son válidos."}

    criterios = _criterios(db)
    ids_validos = {criterio.id for criterio in criterios}
    if not set(pesos).issubset(ids_validos):
        return {"ok": False, "mensaje": "Uno o más criterios no existen."}
    equipos = _equipos_query(db).all()
    actuales = _evaluar_lote(db, equipos, criterios=criterios)
    simulados = _evaluar_lote(db, equipos, criterios=criterios, pesos=pesos)
    detalle = [
        {
            "equipo_id": actual["equipo_id"],
            "nombre": actual["nombre"],
            "estado_actual": actual["estado"],
            "estado_propuesto": simulado["estado"],
        }
        for actual, simulado in zip(actuales, simulados)
        if actual["estado"] != simulado["estado"]
    ]
    cantidad = len(detalle)
    return {
        "ok": True,
        "equipos_cambian_estado": cantidad,
        "cantidad": cantidad,
        "detalle": detalle,
    }


def guardar_pesos(db: Session, pesos: Mapping[Any, Any]) -> dict[str, Any]:
    impacto = impacto_pesos(db, pesos)
    if not impacto.get("ok"):
        return impacto
    criterios = {criterio.id: criterio for criterio in _criterios(db)}
    try:
        for criterio_id, peso in pesos.items():
            criterios[int(criterio_id)].peso = float(_decimal(peso))
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudieron guardar los pesos: {exc}"}
    return {
        "ok": True,
        "mensaje": "Pesos guardados.",
        "equipos_cambian_estado": impacto["equipos_cambian_estado"],
        "impacto": impacto,
        "rubricas": listar_rubricas(db),
    }


def crear_criterio(
    db: Session,
    rubrica_id: int,
    codigo: str,
    nombre: str,
    peso: float,
    orden: int | None = None,
    descripcion: str | None = None,
    que_revisar: str | None = None,
    evidencia_adecuado: str | None = None,
    evidencia_solido: str | None = None,
    evidencia_destacado: str | None = None,
    ejemplo_observacion: str | None = None,
) -> dict[str, Any]:
    peso_decimal = _decimal(peso, Decimal("NaN"))
    if not codigo or not codigo.strip() or not nombre or not nombre.strip():
        return {"ok": False, "mensaje": "El código y el nombre son obligatorios."}
    if not peso_decimal.is_finite() or peso_decimal < 0:
        return {"ok": False, "mensaje": "El peso debe ser un número mayor o igual a cero."}
    rubrica = db.query(Rubrica).filter(Rubrica.id == rubrica_id).first()
    if rubrica is None:
        return {"ok": False, "mensaje": "Rúbrica no encontrada."}
    if db.query(CriterioEvaluacion).filter(CriterioEvaluacion.codigo == codigo.strip()).first():
        return {"ok": False, "mensaje": "Ya existe un criterio con ese código."}
    if orden is None:
        orden = (
            db.query(func.max(CriterioEvaluacion.orden))
            .filter(CriterioEvaluacion.rubrica_id == rubrica_id)
            .scalar()
            or 0
        ) + 1
    try:
        criterio = CriterioEvaluacion(
            rubrica_id=rubrica_id,
            codigo=codigo.strip(),
            nombre=nombre.strip(),
            peso=float(peso_decimal),
            orden=orden,
            activo=True,
            descripcion=str(descripcion).strip() or None if descripcion is not None else None,
            que_revisar=str(que_revisar).strip() or None if que_revisar is not None else None,
            evidencia_adecuado=str(evidencia_adecuado).strip() or None if evidencia_adecuado is not None else None,
            evidencia_solido=str(evidencia_solido).strip() or None if evidencia_solido is not None else None,
            evidencia_destacado=str(evidencia_destacado).strip() or None if evidencia_destacado is not None else None,
            ejemplo_observacion=str(ejemplo_observacion).strip() or None if ejemplo_observacion is not None else None,
        )
        db.add(criterio)
        db.commit()
        db.refresh(criterio)
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo crear el criterio: {exc}"}
    return {
        "ok": True,
        "mensaje": "Criterio creado.",
        "criterio": {
            "id": criterio.id,
            "rubrica_id": criterio.rubrica_id,
            "codigo": criterio.codigo,
            "nombre": criterio.nombre,
            "peso": criterio.peso,
            "orden": criterio.orden,
            "activo": criterio.activo,
            "descripcion": criterio.descripcion,
            "que_revisar": criterio.que_revisar,
            "evidencia_adecuado": criterio.evidencia_adecuado,
            "evidencia_solido": criterio.evidencia_solido,
            "evidencia_destacado": criterio.evidencia_destacado,
            "ejemplo_observacion": criterio.ejemplo_observacion,
        },
    }


_CAMPOS_GUIA_CRITERIO = (
    "descripcion",
    "que_revisar",
    "evidencia_adecuado",
    "evidencia_solido",
    "evidencia_destacado",
    "ejemplo_observacion",
)


def editar_criterio(
    db: Session, criterio_id: int, **campos: Any,
) -> dict[str, Any]:
    """Edita datos del criterio y sus textos de guía sin eliminarlo."""
    criterio = (
        db.query(CriterioEvaluacion)
        .filter(CriterioEvaluacion.id == criterio_id)
        .first()
    )
    if criterio is None:
        return {"ok": False, "mensaje": "Criterio no encontrado."}

    permitidos = {"codigo", "nombre", "peso", "orden", *_CAMPOS_GUIA_CRITERIO}
    cambios = {clave: valor for clave, valor in campos.items() if clave in permitidos}
    if "codigo" in cambios:
        codigo = str(cambios["codigo"] or "").strip()
        if not codigo:
            return {"ok": False, "mensaje": "El código es obligatorio."}
        duplicado = (
            db.query(CriterioEvaluacion)
            .filter(CriterioEvaluacion.codigo == codigo, CriterioEvaluacion.id != criterio_id)
            .first()
        )
        if duplicado:
            return {"ok": False, "mensaje": "Ya existe un criterio con ese código."}
        cambios["codigo"] = codigo
    if "nombre" in cambios:
        nombre = str(cambios["nombre"] or "").strip()
        if not nombre:
            return {"ok": False, "mensaje": "El nombre es obligatorio."}
        cambios["nombre"] = nombre
    if "peso" in cambios:
        peso = _decimal(cambios["peso"], Decimal("NaN"))
        if not peso.is_finite() or peso < 0:
            return {"ok": False, "mensaje": "El peso debe ser un número mayor o igual a cero."}
        cambios["peso"] = float(peso)
    for campo in _CAMPOS_GUIA_CRITERIO:
        if campo in cambios:
            cambios[campo] = str(cambios[campo]).strip() or None if cambios[campo] is not None else None
    try:
        for campo, valor in cambios.items():
            setattr(criterio, campo, valor)
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo actualizar el criterio: {exc}"}
    return {"ok": True, "mensaje": "Criterio actualizado.", "criterio_id": criterio.id}


def guardar_guia_criterio(
    db: Session, criterio_id: int, **campos: Any,
) -> dict[str, Any] | None:
    """Guarda textos guía parciales; devuelve None si no queda guía cargada."""
    criterio = (
        db.query(CriterioEvaluacion)
        .filter(CriterioEvaluacion.id == criterio_id)
        .first()
    )
    if criterio is None:
        return None
    for campo in _CAMPOS_GUIA_CRITERIO:
        if campo in campos:
            valor = campos[campo]
            setattr(criterio, campo, str(valor).strip() or None if valor is not None else None)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo guardar la guía del criterio: {exc}"}
    guia = {campo: getattr(criterio, campo) for campo in _CAMPOS_GUIA_CRITERIO if getattr(criterio, campo)}
    return guia or None


def guardar_guia_rubrica(
    db: Session, rubrica_id: int, guia: str | None,
) -> dict[str, Any] | None:
    """Guarda la guía de rúbrica y devuelve None cuando queda vacía."""
    rubrica = db.query(Rubrica).filter(Rubrica.id == rubrica_id).first()
    if rubrica is None:
        return None
    rubrica.guia = str(guia).strip() or None if guia is not None else None
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo guardar la guía de la rúbrica: {exc}"}
    return {"guia": rubrica.guia} if rubrica.guia else None


def activar_criterio(db: Session, id: int, activo: bool) -> dict[str, Any]:
    criterio = db.query(CriterioEvaluacion).filter(CriterioEvaluacion.id == id).first()
    if criterio is None:
        return {"ok": False, "mensaje": "Criterio no encontrado."}
    criterio.activo = bool(activo)
    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo actualizar el criterio: {exc}"}
    return {"ok": True, "mensaje": "Estado del criterio actualizado.", "criterio_id": id, "activo": criterio.activo}


def guardar_umbrales(
    db: Session, total_pct: float, rubrica_pct: float,
) -> dict[str, Any]:
    total = _decimal(total_pct, Decimal("NaN"))
    rubrica = _decimal(rubrica_pct, Decimal("NaN"))
    if (
        not total.is_finite() or not rubrica.is_finite()
        or not 0 <= total <= 100 or not 0 <= rubrica <= 100
    ):
        return {"ok": False, "mensaje": "Los umbrales deben estar entre 0 y 100."}
    filas = {fila.clave: fila for fila in db.query(ConfigEvaluacion).all()}
    try:
        for clave, valor in (
            ("umbral_total_pct", total),
            ("umbral_rubrica_pct", rubrica),
        ):
            if clave in filas:
                filas[clave].valor = str(valor)
            else:
                db.add(ConfigEvaluacion(clave=clave, valor=str(valor)))
        db.commit()
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudieron guardar los umbrales: {exc}"}
    return {
        "ok": True,
        "mensaje": "Umbrales guardados.",
        "total_pct": _mostrar(total),
        "rubrica_pct": _mostrar(rubrica),
    }


def previsualizar_importacion_csv(db: Session, archivo_bytes: bytes) -> dict[str, Any]:
    """Punto de entrada de servicio para validar un CSV sin persistir cambios."""
    from app.services.evaluacion_csv import previsualizar_csv

    return previsualizar_csv(db, archivo_bytes)


def aplicar_importacion_csv(db: Session, archivo_bytes: bytes, modo: str) -> dict[str, Any]:
    """Punto de entrada de servicio para aplicar una importación CSV."""
    from app.services.evaluacion_csv import aplicar_csv

    return aplicar_csv(db, archivo_bytes, modo)


def obtener_plantilla_csv(
    db: Session, cohorte_id: int | None = None, comision_id: int | None = None,
) -> str:
    """Genera la plantilla CSV desde el servicio de evaluación."""
    from app.services.evaluacion_csv import plantilla_csv

    return plantilla_csv(db, cohorte_id, comision_id)


def exportar_evaluaciones_csv(
    db: Session,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    estado: str | None = None,
) -> str:
    """Exporta evaluaciones a CSV desde el servicio de evaluación."""
    from app.services.evaluacion_csv import exportar_csv

    return exportar_csv(db, cohorte_id, comision_id, estado)
