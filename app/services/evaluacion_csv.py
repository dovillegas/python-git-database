"""Importación y exportación CSV para las evaluaciones por rúbrica."""

from __future__ import annotations

import csv
import io
import unicodedata
from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    Equipo,
    Evaluacion,
    Rubrica,
    Valoracion,
)
from app.services.evaluacion_service import _criterios, _equipos_query, _evaluar_lote


_ETIQUETAS_NIVEL = {
    "no evidencia": 0,
    "insuficiente": 1,
    "basico": 2,
    "adecuado": 3,
    "solido": 4,
    "destacado": 5,
}
_COLUMNAS_BASE = {"cohorte", "comision", "equipo"}


def _normalizar(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or ""))
    texto = "".join(caracter for caracter in texto if not unicodedata.combining(caracter))
    return " ".join(texto.strip().casefold().split())


def _leer_csv(archivo_bytes: bytes) -> tuple[list[str], list[list[str]], list[dict[str, Any]]]:
    try:
        texto = archivo_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        texto = archivo_bytes.decode("latin-1")
    if not texto.strip():
        return [], [], [{"fila": 1, "columna": "archivo", "mensaje": "El CSV está vacío."}]

    try:
        separador = csv.Sniffer().sniff(texto[:8192], delimiters=",;").delimiter
    except csv.Error:
        muestra = texto[:8192]
        separador = ";" if muestra.count(";") > muestra.count(",") else ","
    try:
        filas = list(csv.reader(io.StringIO(texto, newline=""), delimiter=separador))
    except csv.Error as exc:
        return [], [], [{"fila": 1, "columna": "archivo", "mensaje": f"CSV inválido: {exc}"}]
    if not filas:
        return [], [], [{"fila": 1, "columna": "archivo", "mensaje": "El CSV no contiene cabecera."}]

    encabezados = [celda.strip().lstrip("\ufeff") for celda in filas[0]]
    errores: list[dict[str, Any]] = []
    vistos: set[str] = set()
    for encabezado in encabezados:
        clave = _normalizar(encabezado)
        if clave in vistos:
            errores.append({"fila": 1, "columna": encabezado, "mensaje": "La cabecera está duplicada."})
        vistos.add(clave)
    return encabezados, filas[1:], errores


def _valor_celda(fila: list[str], indice: int | None) -> str:
    return fila[indice].strip() if indice is not None and indice < len(fila) else ""


def _parsear_nivel(valor: str) -> tuple[int | None, str | None]:
    celda = valor.strip()
    if not celda:
        return None, None
    if celda.isdecimal():
        nivel = int(celda)
        if 0 <= nivel <= 5:
            return nivel, None
        return None, "El nivel debe estar entre 0 y 5."
    nivel = _ETIQUETAS_NIVEL.get(_normalizar(celda))
    if nivel is None:
        return None, "Valor inválido: use un entero de 0 a 5 o una etiqueta reconocida."
    return nivel, None


def _comision_coincide(valor: str, numero: int) -> bool:
    valor = valor.strip()
    try:
        return int(valor) == numero
    except ValueError:
        return valor == str(numero)


def _planificar(
    db: Session, archivo_bytes: bytes,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    encabezados, filas_csv, errores = _leer_csv(archivo_bytes)
    advertencias: list[dict[str, Any]] = []
    if not encabezados:
        return [], errores, advertencias

    indices = {_normalizar(nombre): indice for indice, nombre in enumerate(encabezados)}
    for requerida in ("cohorte", "equipo"):
        if requerida not in indices:
            errores.append({
                "fila": 1,
                "columna": requerida,
                "mensaje": f"Falta la columna obligatoria '{requerida}'.",
            })

    criterios_activos = [criterio for criterio in _criterios(db) if criterio.activo]
    criterio_por_codigo = {_normalizar(criterio.codigo): criterio for criterio in criterios_activos}
    indices_criterio: dict[int, dict[str, int]] = defaultdict(dict)
    for indice, encabezado in enumerate(encabezados):
        clave = _normalizar(encabezado)
        if clave in _COLUMNAS_BASE:
            continue
        observacion = clave.endswith("_obs")
        codigo = clave[:-4] if observacion else clave
        criterio = criterio_por_codigo.get(codigo)
        if criterio is None:
            advertencias.append({
                "fila": 1,
                "columna": encabezado,
                "mensaje": "Columna de criterio desconocido; se ignora.",
            })
            continue
        tipo = "observacion" if observacion else "nivel"
        if tipo in indices_criterio[criterio.id]:
            errores.append({"fila": 1, "columna": encabezado, "mensaje": "Columna de criterio duplicada."})
        indices_criterio[criterio.id][tipo] = indice

    if any(error["fila"] == 1 for error in errores):
        return [], errores, advertencias

    equipos_por_clave: dict[tuple[str, str], list[Equipo]] = defaultdict(list)
    for equipo in _equipos_query(db).all():
        equipos_por_clave[(str(equipo.comision.cohorte.anio), _normalizar(equipo.nombre))].append(equipo)
    evaluacion_por_equipo = {
        evaluacion.equipo_id: evaluacion
        for evaluacion in db.query(Evaluacion).all()
    }
    valoraciones_por_clave: dict[tuple[int, int], Valoracion] = {}
    for valoracion in db.query(Valoracion).all():
        valoraciones_por_clave[(valoracion.evaluacion_id, valoracion.criterio_id)] = valoracion

    planes: list[dict[str, Any]] = []
    for numero_fila, fila_csv in enumerate(filas_csv, start=2):
        if not any(celda.strip() for celda in fila_csv):
            continue
        errores_fila: list[dict[str, Any]] = []
        cohorte = _valor_celda(fila_csv, indices.get("cohorte"))
        nombre_equipo = _valor_celda(fila_csv, indices.get("equipo"))
        comision_csv = _valor_celda(fila_csv, indices.get("comision"))
        if not cohorte:
            errores_fila.append({"fila": numero_fila, "columna": "cohorte", "mensaje": "La cohorte es obligatoria."})
        if not nombre_equipo:
            errores_fila.append({"fila": numero_fila, "columna": "equipo", "mensaje": "El equipo es obligatorio."})

        candidatos = equipos_por_clave.get((cohorte, _normalizar(nombre_equipo)), [])
        equipo = None
        if cohorte and nombre_equipo:
            if not candidatos:
                errores_fila.append({
                    "fila": numero_fila,
                    "columna": "equipo",
                    "mensaje": "No existe un equipo con esa cohorte y nombre.",
                })
            elif len(candidatos) > 1:
                errores_fila.append({
                    "fila": numero_fila,
                    "columna": "equipo",
                    "mensaje": "Equipo ambiguo para esa cohorte y nombre.",
                })
            else:
                equipo = candidatos[0]
                if comision_csv and not _comision_coincide(comision_csv, equipo.comision.numero):
                    errores_fila.append({
                        "fila": numero_fila,
                        "columna": "comision",
                        "mensaje": f"La comisión indicada no coincide con la comisión {equipo.comision.numero} del equipo.",
                    })
                    equipo = None

        cambios: list[dict[str, Any]] = []
        for criterio in criterios_activos:
            columnas = indices_criterio.get(criterio.id, {})
            indice_nivel = columnas.get("nivel")
            indice_obs = columnas.get("observacion")
            texto_nivel = _valor_celda(fila_csv, indice_nivel)
            texto_obs = _valor_celda(fila_csv, indice_obs)
            nivel, error_nivel = _parsear_nivel(texto_nivel)
            if error_nivel:
                errores_fila.append({
                    "fila": numero_fila,
                    "columna": encabezados[indice_nivel] if indice_nivel is not None else criterio.codigo,
                    "mensaje": error_nivel,
                })
                continue
            if not texto_nivel and not texto_obs:
                continue

            valoracion_actual = None
            if equipo is not None:
                evaluacion_actual = evaluacion_por_equipo.get(equipo.id)
                if evaluacion_actual is not None:
                    valoracion_actual = valoraciones_por_clave.get((evaluacion_actual.id, criterio.id))
            if not texto_nivel and texto_obs and valoracion_actual is None:
                advertencias.append({
                    "fila": numero_fila,
                    "columna": encabezados[indice_obs] if indice_obs is not None else f"{criterio.codigo}_obs",
                    "mensaje": "La observación se ignora porque el criterio todavía no tiene nivel.",
                })
            cambios.append({
                "criterio_id": criterio.id,
                "codigo": criterio.codigo,
                "nivel": nivel,
                "nivel_informado": bool(texto_nivel),
                "observacion": texto_obs,
                "observacion_informada": bool(texto_obs),
                "valoracion_existente": valoracion_actual is not None,
            })
        errores.extend(errores_fila)
        planes.append({
            "fila": numero_fila,
            "equipo": equipo,
            "cohorte": cohorte,
            "nombre_equipo": nombre_equipo,
            "cambios": cambios,
            "errores": errores_fila,
        })
    return planes, errores, advertencias


def previsualizar_csv(db: Session, archivo_bytes: bytes) -> dict[str, Any]:
    """Valida un CSV y devuelve el plan de cambios sin realizar escrituras."""
    try:
        planes, errores, advertencias = _planificar(db, archivo_bytes)
    except Exception as exc:
        return {
            "ok": False,
            "equipos_procesados": 0,
            "valoraciones_creadas": 0,
            "valoraciones_actualizadas": 0,
            "errores": [{"fila": 1, "columna": "archivo", "mensaje": str(exc)}],
            "advertencias": [],
            "filas": [],
        }
    filas = []
    for plan in planes:
        equipo = plan["equipo"]
        filas.append({
            "fila": plan["fila"],
            "equipo_id": equipo.id if equipo else None,
            "cohorte": equipo.comision.cohorte.anio if equipo else plan["cohorte"],
            "comision": equipo.comision.numero if equipo else None,
            "equipo": equipo.nombre if equipo else plan["nombre_equipo"],
            "cambios": plan["cambios"],
            "errores": plan["errores"],
        })
    return {
        "ok": not errores,
        "equipos_procesados": sum(1 for plan in planes if plan["equipo"] is not None),
        "valoraciones_creadas": 0,
        "valoraciones_actualizadas": 0,
        "errores": errores,
        "advertencias": advertencias,
        "filas": filas,
    }


def aplicar_csv(db: Session, archivo_bytes: bytes, modo: str) -> dict[str, Any]:
    """Aplica las celdas válidas y confirma cada fila/equipo independientemente."""
    if modo not in {"sobrescribir", "solo_vacios"}:
        return {
            "ok": False,
            "equipos_procesados": 0,
            "valoraciones_creadas": 0,
            "valoraciones_actualizadas": 0,
            "errores": [{"fila": 1, "columna": "modo", "mensaje": "El modo debe ser 'sobrescribir' o 'solo_vacios'."}],
            "advertencias": [],
        }
    try:
        planes, errores, advertencias = _planificar(db, archivo_bytes)
    except Exception as exc:
        return {
            "ok": False,
            "equipos_procesados": 0,
            "valoraciones_creadas": 0,
            "valoraciones_actualizadas": 0,
            "errores": [{"fila": 1, "columna": "archivo", "mensaje": str(exc)}],
            "advertencias": [],
        }

    procesados = creadas = actualizadas = 0
    for plan in planes:
        equipo = plan["equipo"]
        if equipo is None:
            continue
        creadas_fila = actualizadas_fila = 0
        try:
            evaluacion = db.query(Evaluacion).filter(Evaluacion.equipo_id == equipo.id).first()
            for cambio in plan["cambios"]:
                existente = None
                if evaluacion is not None:
                    existente = (
                        db.query(Valoracion)
                        .filter(
                            Valoracion.evaluacion_id == evaluacion.id,
                            Valoracion.criterio_id == cambio["criterio_id"],
                        )
                        .first()
                    )
                nivel_modificable = cambio["nivel_informado"] and (
                    existente is None or modo == "sobrescribir"
                )
                if nivel_modificable and evaluacion is None:
                    evaluacion = Evaluacion(equipo_id=equipo.id, evaluador="Importación CSV")
                    db.add(evaluacion)
                    db.flush()
                if nivel_modificable and existente is None:
                    db.add(Valoracion(
                        evaluacion_id=evaluacion.id,
                        criterio_id=cambio["criterio_id"],
                        nivel=cambio["nivel"],
                        observacion=cambio["observacion"] or None,
                    ))
                    creadas_fila += 1
                    continue

                if existente is not None:
                    cambio_realizado = False
                    if nivel_modificable and existente.nivel != cambio["nivel"]:
                        existente.nivel = cambio["nivel"]
                        cambio_realizado = True
                    # Una observación no vacía se actualiza aunque el modo sea solo_vacios.
                    if cambio["observacion_informada"] and existente.observacion != cambio["observacion"]:
                        existente.observacion = cambio["observacion"]
                        cambio_realizado = True
                    if cambio_realizado:
                        actualizadas_fila += 1
            db.commit()
            procesados += 1
            creadas += creadas_fila
            actualizadas += actualizadas_fila
        except Exception as exc:
            db.rollback()
            errores.append({
                "fila": plan["fila"],
                "columna": "equipo",
                "mensaje": f"No se pudieron guardar los cambios del equipo: {exc}",
            })
    return {
        "ok": not errores,
        "modo": modo,
        "equipos_procesados": procesados,
        "valoraciones_creadas": creadas,
        "valoraciones_actualizadas": actualizadas,
        "errores": errores,
        "advertencias": advertencias,
    }


def plantilla_csv(
    db: Session, cohorte_id: int | None = None, comision_id: int | None = None,
) -> str:
    """Devuelve una plantilla CSV con una fila por equipo y criterios activos."""
    criterios = [criterio for criterio in _criterios(db) if criterio.activo]
    equipos = _equipos_query(db, cohorte_id, comision_id).all()
    evaluaciones = {
        evaluacion.equipo_id: evaluacion
        for evaluacion in db.query(Evaluacion).filter(
            Evaluacion.equipo_id.in_([equipo.id for equipo in equipos])
        ).all()
    } if equipos else {}
    eval_ids = [evaluacion.id for evaluacion in evaluaciones.values()]
    valoraciones = db.query(Valoracion).filter(
        Valoracion.evaluacion_id.in_(eval_ids)
    ).all() if eval_ids else []
    valores = {(valor.evaluacion_id, valor.criterio_id): valor for valor in valoraciones}

    salida = io.StringIO(newline="")
    escritor = csv.writer(salida, lineterminator="\n")
    cabecera = ["cohorte", "comision", "equipo"]
    for criterio in criterios:
        cabecera.extend((criterio.codigo, f"{criterio.codigo}_obs"))
    escritor.writerow(cabecera)
    for equipo in equipos:
        evaluacion = evaluaciones.get(equipo.id)
        fila: list[Any] = [equipo.comision.cohorte.anio, equipo.comision.numero, equipo.nombre]
        for criterio in criterios:
            valor = valores.get((evaluacion.id, criterio.id)) if evaluacion else None
            fila.extend((valor.nivel if valor else "", (valor.observacion or "") if valor else ""))
        escritor.writerow(fila)
    return salida.getvalue()


def exportar_csv(
    db: Session,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    estado: str | None = None,
) -> str:
    """Exporta niveles, observaciones y resultados calculados de los equipos."""
    criterios = [criterio for criterio in _criterios(db) if criterio.activo]
    equipos = _equipos_query(db, cohorte_id, comision_id).all()
    resultados = {dato["equipo_id"]: dato for dato in _evaluar_lote(db, equipos)}
    if estado in {"aprobado", "no_aprobado", "parcial", "sin_evaluar"}:
        equipos = [equipo for equipo in equipos if resultados[equipo.id]["estado"] == estado]
    evaluaciones = {
        evaluacion.equipo_id: evaluacion
        for evaluacion in db.query(Evaluacion).filter(
            Evaluacion.equipo_id.in_([equipo.id for equipo in equipos])
        ).all()
    } if equipos else {}
    eval_ids = [evaluacion.id for evaluacion in evaluaciones.values()]
    valoraciones = db.query(Valoracion).filter(
        Valoracion.evaluacion_id.in_(eval_ids)
    ).all() if eval_ids else []
    valores = {(valor.evaluacion_id, valor.criterio_id): valor for valor in valoraciones}

    rubricas: dict[int, Rubrica] = {}
    for criterio in criterios:
        rubricas[criterio.rubrica_id] = criterio.rubrica
    rubricas_ordenadas = sorted(rubricas.values(), key=lambda rubrica: (rubrica.orden, rubrica.id))
    salida = io.StringIO(newline="")
    escritor = csv.writer(salida, lineterminator="\n")
    cabecera = ["cohorte", "comision", "equipo"]
    for criterio in criterios:
        cabecera.extend((criterio.codigo, f"{criterio.codigo}_obs", f"{criterio.codigo}_puntaje"))
    for rubrica in rubricas_ordenadas:
        cabecera.extend((f"rubrica_{rubrica.codigo}_puntaje", f"rubrica_{rubrica.codigo}_porcentaje"))
    cabecera.extend(("total_puntaje", "total_maximo", "total_porcentaje", "estado"))
    escritor.writerow(cabecera)

    for equipo in equipos:
        evaluacion = evaluaciones.get(equipo.id)
        resultado = resultados[equipo.id]
        puntajes = {item["criterio_id"]: item["puntaje"] for item in resultado["criterios"]}
        resultado_rubricas = {item["rubrica_id"]: item for item in resultado["rubricas"]}
        fila: list[Any] = [equipo.comision.cohorte.anio, equipo.comision.numero, equipo.nombre]
        for criterio in criterios:
            valor = valores.get((evaluacion.id, criterio.id)) if evaluacion else None
            fila.extend((
                valor.nivel if valor else "",
                (valor.observacion or "") if valor else "",
                puntajes.get(criterio.id),
            ))
        for rubrica in rubricas_ordenadas:
            dato = resultado_rubricas.get(rubrica.id, {})
            fila.extend((dato.get("puntaje"), dato.get("pct")))
        fila.extend((resultado["total"], resultado["maximo_total"], resultado["pct_total"], resultado["estado"]))
        escritor.writerow(fila)
    return salida.getvalue()