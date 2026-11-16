"""Importación idempotente de estudiantes desde CSV y Excel."""

from __future__ import annotations

import math
import re
import unicodedata
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Cohorte, Comision, Equipo, Estudiante
from app.services import pipeline_service


_COLUMNAS_REQUERIDAS = {
    "cohorte",
    "comision",
    "equipo",
    "proyecto_nombre",
    "url_github",
    "apellido_nombre",
    "dni",
}


def _normalizar_columna(columna: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(columna).strip().lower())
    texto = "".join(caracter for caracter in texto if not unicodedata.combining(caracter))
    return re.sub(r"\s+", "_", texto.lstrip("\ufeff"))


def _es_vacio(valor: Any) -> bool:
    if valor is None:
        return True
    try:
        return bool(pd.isna(valor))
    except (TypeError, ValueError):
        return False


def _texto(valor: Any) -> str:
    if _es_vacio(valor):
        return ""
    if isinstance(valor, float) and math.isfinite(valor) and valor.is_integer():
        return str(int(valor))
    texto = str(valor).strip()
    return re.sub(r"^([+-]?\d+)\.0+$", r"\1", texto)


def _entero(valor: Any, nombre: str) -> int:
    texto = _texto(valor)
    if not re.fullmatch(r"\d+", texto):
        raise ValueError(f"'{nombre}' debe ser un número entero")
    return int(texto)


def _leer_archivo(path: str | Path) -> pd.DataFrame:
    archivo = Path(path)
    extension = archivo.suffix.lower()
    if extension == ".csv":
        try:
            dataframe = pd.read_csv(
                archivo,
                sep=None,
                engine="python",
                encoding="utf-8-sig",
                dtype=str,
                keep_default_na=False,
            )
        except UnicodeDecodeError:
            dataframe = pd.read_csv(
                archivo,
                sep=None,
                engine="python",
                encoding="latin-1",
                dtype=str,
                keep_default_na=False,
            )
    elif extension in {".xlsx", ".xls"}:
        dataframe = pd.read_excel(archivo, dtype=str, keep_default_na=False)
    else:
        raise ValueError("Formato no soportado: use un archivo .csv, .xlsx o .xls")
    return dataframe.apply(lambda columna: columna.map(_texto))


def _obtener_equipo(
    db: Session,
    cohorte_id: int,
    comision_id: int,
    equipo_nombre: str,
    proyecto_nombre: str | None,
    url_github: str,
) -> tuple[Equipo | None, Comision | None, bool, bool]:
    equipos = (
        db.query(Equipo, Comision)
        .join(Comision, Equipo.comision_id == Comision.id)
        .filter(Comision.cohorte_id == cohorte_id)
        .order_by(Comision.numero, Comision.id, Equipo.id)
        .all()
    )
    nombre_normalizado = equipo_nombre.strip().casefold()
    equipo = next(
        (
            (actual, comision)
            for actual, comision in equipos
            if actual.nombre.strip().casefold() == nombre_normalizado
        ),
        None,
    )

    comision_equipo = equipo[1] if equipo else None
    equipo = equipo[0] if equipo else None
    if equipo is None:
        if comision_id <= 0:
            return None, None, False, False
        equipo = Equipo(
            comision_id=comision_id,
            nombre=equipo_nombre,
            nombre_proyecto=proyecto_nombre,
            url_github=url_github,
            estado_salud="sin_verificar",
        )
        db.add(equipo)
        return equipo, None, True, False

    actualizado = False
    if proyecto_nombre is not None and equipo.nombre_proyecto != proyecto_nombre:
        equipo.nombre_proyecto = proyecto_nombre
        actualizado = True
    return equipo, comision_equipo, False, actualizado


def _normalizar_url(valor: Any) -> str:
    texto = _texto(valor).strip().rstrip("/")
    if texto.casefold().endswith(".git"):
        texto = texto[:-4].rstrip("/")
    return texto


def _dni_parcial(dni: str) -> str:
    """Devuelve una máscara que nunca expone el DNI completo."""
    if len(dni) <= 2:
        return "*"
    return "*" * (len(dni) - 2) + dni[-2:]


def _nuevo_resumen() -> dict[str, Any]:
    return {
        "cohortes": {"creadas": 0, "actualizadas": 0},
        "equipos": {"creados": 0, "actualizados": 0},
        "estudiantes": {"creados": 0, "actualizados": 0},
        "filas_procesadas": 0,
        "filas_con_error": [],
        "conflictos": [],
        "estudiantes_nuevos": [],
    }


def _buscar_estudiante(db: Session, dni: str, email: str | None) -> Estudiante | None:
    estudiante = db.query(Estudiante).filter(Estudiante.dni == dni).first()
    if estudiante is None and email:
        estudiante = (
            db.query(Estudiante)
            .filter(func.lower(Estudiante.email) == email.casefold())
            .first()
        )
    return estudiante


def importar_archivo(
    db: Session,
    path: str | Path,
    modo: str = "conservar_panel",
    solo_previsualizar: bool = False,
) -> dict[str, Any]:
    """Importa estudiantes y sus entidades relacionadas desde un archivo.

    Las filas se aíslan mediante savepoints: una fila inválida se informa en el
    resumen sin descartar las demás. Los cambios válidos se confirman juntos.
    """
    resumen = _nuevo_resumen()
    if modo not in {"conservar_panel", "archivo_manda"}:
        resumen["filas_con_error"].append(
            {"fila": None, "error": "Modo inválido: use 'conservar_panel' o 'archivo_manda'."}
        )
        if solo_previsualizar:
            db.rollback()
        return resumen

    try:
        estado_pipeline = pipeline_service.estado()
    except Exception as error:
        estado_pipeline = {"activo": True}
        mensaje_pipeline = f"No se pudo verificar el proceso activo: {error}"
    else:
        mensaje_pipeline = "Hay una sincronización activa; la importación fue rechazada."
    if estado_pipeline.get("activo") is True:
        resumen["filas_con_error"].append({"fila": None, "error": mensaje_pipeline})
        if solo_previsualizar:
            db.rollback()
        return resumen

    try:
        dataframe = _leer_archivo(path)
        dataframe.columns = [_normalizar_columna(columna) for columna in dataframe.columns]
        if dataframe.columns.duplicated().any():
            raise ValueError("El archivo contiene nombres de columnas duplicados")
        columnas_faltantes = sorted(_COLUMNAS_REQUERIDAS - set(dataframe.columns))
        if columnas_faltantes:
            raise ValueError("Faltan columnas requeridas: " + ", ".join(columnas_faltantes))
    except Exception as error:
        resumen["filas_con_error"].append({"fila": None, "error": str(error)})
        if solo_previsualizar:
            db.rollback()
        return resumen

    # Se validan todas las ubicaciones antes de importar para no dar prioridad
    # accidental a la primera comisión encontrada en un mismo archivo.
    comisiones_por_equipo: dict[tuple[int, str], set[int]] = {}
    filas_por_equipo: dict[tuple[int, str], list[int]] = {}
    for numero_fila, (_, fila) in enumerate(dataframe.iterrows(), start=2):
        try:
            anio = _entero(fila["cohorte"], "cohorte")
            numero = _entero(fila["comision"], "comision")
            nombre = _texto(fila["equipo"]).strip().casefold()
            if nombre:
                clave = (anio, nombre)
                comisiones_por_equipo.setdefault(clave, set()).add(numero)
                filas_por_equipo.setdefault(clave, []).append(numero_fila)
        except (KeyError, ValueError, TypeError):
            continue
    filas_equipo_ambiguo = {
        numero_fila
        for clave, numeros in comisiones_por_equipo.items()
        if len(numeros) > 1
        for numero_fila in filas_por_equipo[clave]
    }

    # El modo legacy del driver sqlite3 no inicia una transacción física al
    # abrir un SAVEPOINT. Iniciar BEGIN evita que RELEASE confirme la fila antes
    # de que solo_previsualizar pueda revertirla.
    if db.get_bind().dialect.name == "sqlite":
        conexion = db.connection()
        conexion_dbapi = conexion.connection.driver_connection
        if not conexion_dbapi.in_transaction:
            conexion.exec_driver_sql("BEGIN")

    dnis_vistos: dict[str, int] = {}

    for numero_fila, (_, fila) in enumerate(dataframe.iterrows(), start=2):
        if numero_fila in filas_equipo_ambiguo:
            resumen["filas_con_error"].append(
                {
                    "fila": numero_fila,
                    "error": "nombre de equipo repetido en comisiones distintas",
                }
            )
            continue
        cambios = {
            "cohorte_creada": False,
            "equipo_creado": False,
            "equipo_actualizado": False,
            "estudiante_creado": False,
            "estudiante_actualizado": False,
        }
        conflictos_fila: list[dict[str, Any]] = []
        estudiante_nuevo: dict[str, str] | None = None

        try:
            dni = _texto(fila["dni"])
            if dni:
                if dni in dnis_vistos:
                    raise ValueError(
                        f"DNI duplicado en el archivo (ya aparece en la fila {dnis_vistos[dni]})"
                    )
                dnis_vistos[dni] = numero_fila

            cohorte_anio = _entero(fila["cohorte"], "cohorte")
            comision_numero = _entero(fila["comision"], "comision")
            equipo_etiqueta = _texto(fila["equipo"])
            proyecto_nombre = _texto(fila["proyecto_nombre"]) or None
            if proyecto_nombre == "-":
                proyecto_nombre = None
            url_github_original = _texto(fila["url_github"])
            url_github = _normalizar_url(url_github_original)
            apellido_nombre = _texto(fila["apellido_nombre"])
            email = (
                _texto(fila["email"]) or None
                if "email" in dataframe.columns
                else None
            )
            usuario_github = (
                _texto(fila["usuario_github"]) or None
                if "usuario_github" in dataframe.columns
                else None
            )

            requeridos = {
                "equipo": equipo_etiqueta,
                "url_github": url_github,
                "apellido_nombre": apellido_nombre,
                "dni": dni,
            }
            vacios = [nombre for nombre, valor in requeridos.items() if not valor]
            if vacios:
                raise ValueError("Valores requeridos vacíos: " + ", ".join(vacios))

            with db.begin_nested():
                cohorte = db.query(Cohorte).filter(Cohorte.anio == cohorte_anio).first()
                if cohorte is None:
                    cohorte = Cohorte(anio=cohorte_anio)
                    db.add(cohorte)
                    db.flush()
                    cambios["cohorte_creada"] = True

                comision_archivo = (
                    db.query(Comision)
                    .filter(
                        Comision.cohorte_id == cohorte.id,
                        Comision.numero == comision_numero,
                    )
                    .first()
                )
                equipo, comision_actual, equipo_creado, equipo_actualizado = _obtener_equipo(
                    db,
                    cohorte.id,
                    comision_archivo.id if comision_archivo else -1,
                    equipo_etiqueta,
                    proyecto_nombre,
                    url_github,
                )
                if equipo is None:
                    comision_archivo = Comision(
                        cohorte_id=cohorte.id,
                        numero=comision_numero,
                    )
                    db.add(comision_archivo)
                    db.flush()
                    equipo, comision_actual, equipo_creado, equipo_actualizado = _obtener_equipo(
                        db,
                        cohorte.id,
                        comision_archivo.id,
                        equipo_etiqueta,
                        proyecto_nombre,
                        url_github,
                    )
                db.flush()
                cambios["equipo_creado"] = equipo_creado
                cambios["equipo_actualizado"] = equipo_actualizado

                if comision_actual is not None and comision_actual.numero != comision_numero:
                    conflictos_fila.append(
                        {
                            "tipo": "comision_distinta",
                            "fila": numero_fila,
                            "equipo": equipo.nombre,
                            "comision_actual": comision_actual.numero,
                            "comision_archivo": comision_numero,
                        }
                    )

                url_actual = _normalizar_url(equipo.url_github)
                if url_actual and url_actual != url_github:
                    if modo == "archivo_manda":
                        equipo.url_github = url_github
                        cambios["equipo_actualizado"] = True
                    else:
                        conflictos_fila.append(
                            {
                                "tipo": "url_distinta",
                                "equipo": equipo.nombre,
                                "actual": equipo.url_github,
                                "archivo": url_github,
                            }
                        )
                elif not url_actual:
                    equipo.url_github = url_github
                    cambios["equipo_actualizado"] = True

                estudiante = _buscar_estudiante(db, dni, email)
                if estudiante is None:
                    db.add(
                        Estudiante(
                            equipo_id=equipo.id,
                            apellido_nombre=apellido_nombre,
                            dni=dni,
                            email=email,
                            usuario_github=usuario_github,
                        )
                    )
                    cambios["estudiante_creado"] = True
                    estudiante_nuevo = {
                        "equipo": equipo.nombre,
                        "nombre": apellido_nombre,
                    }
                else:
                    if estudiante.equipo_id != equipo.id and modo != "archivo_manda":
                        equipo_actual = (
                            db.query(Equipo)
                            .filter(Equipo.id == estudiante.equipo_id)
                            .first()
                        )
                        conflictos_fila.append(
                            {
                                "tipo": "estudiante_en_otro_equipo",
                                "fila": numero_fila,
                                "dni_parcial": _dni_parcial(dni),
                                "equipo_actual": equipo_actual.nombre if equipo_actual else None,
                                "equipo_archivo": equipo.nombre,
                            }
                        )
                    else:
                        actualizado = False
                        for atributo, valor in (
                            ("equipo_id", equipo.id),
                            ("apellido_nombre", apellido_nombre),
                            ("dni", dni),
                        ):
                            if getattr(estudiante, atributo) != valor:
                                setattr(estudiante, atributo, valor)
                                actualizado = True
                        for atributo, valor in (
                            ("email", email),
                            ("usuario_github", usuario_github),
                        ):
                            if valor is not None and getattr(estudiante, atributo) != valor:
                                setattr(estudiante, atributo, valor)
                                actualizado = True
                        cambios["estudiante_actualizado"] = actualizado
                db.flush()

            resumen["conflictos"].extend(conflictos_fila)
            if estudiante_nuevo is not None:
                resumen["estudiantes_nuevos"].append(estudiante_nuevo)
            if cambios["cohorte_creada"]:
                resumen["cohortes"]["creadas"] += 1
            if cambios["equipo_creado"]:
                resumen["equipos"]["creados"] += 1
            elif cambios["equipo_actualizado"]:
                resumen["equipos"]["actualizados"] += 1
            if cambios["estudiante_creado"]:
                resumen["estudiantes"]["creados"] += 1
            elif cambios["estudiante_actualizado"]:
                resumen["estudiantes"]["actualizados"] += 1
            resumen["filas_procesadas"] += 1
        except Exception as error:
            resumen["filas_con_error"].append(
                {"fila": numero_fila, "error": str(error)}
            )

    try:
        if solo_previsualizar:
            db.rollback()
        else:
            db.commit()
    except Exception as error:
        db.rollback()
        resumen["filas_con_error"].append(
            {"fila": None, "error": f"No se pudo guardar la importación: {error}"}
        )
    return resumen