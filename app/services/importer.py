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
    return str(valor).strip()


def _entero(valor: Any, nombre: str) -> int:
    texto = _texto(valor)
    if not re.fullmatch(r"\d+(?:\.0+)?", texto):
        raise ValueError(f"'{nombre}' debe ser un número entero")
    return int(float(texto))


def _leer_archivo(path: str | Path) -> pd.DataFrame:
    archivo = Path(path)
    extension = archivo.suffix.lower()
    if extension == ".csv":
        try:
            return pd.read_csv(archivo, sep=None, engine="python", encoding="utf-8-sig", dtype=str)
        except UnicodeDecodeError:
            return pd.read_csv(archivo, sep=None, engine="python", encoding="latin-1", dtype=str)
    if extension in {".xlsx", ".xls"}:
        return pd.read_excel(archivo, dtype=object)
    raise ValueError("Formato no soportado: use un archivo .csv, .xlsx o .xls")


def _clave_url(url: str) -> str:
    return url.strip().casefold().rstrip("/").removesuffix(".git")


def _obtener_equipo(
    db: Session,
    comision_id: int,
    proyecto_nombre: str,
    url_github: str,
) -> tuple[Equipo, bool, bool]:
    equipos = db.query(Equipo).filter(Equipo.comision_id == comision_id).all()
    clave_url = _clave_url(url_github)
    equipo = next(
        (actual for actual in equipos if _clave_url(actual.url_github) == clave_url),
        None,
    )
    if equipo is None:
        coincidencias_nombre = [
            actual
            for actual in equipos
            if actual.nombre_proyecto.strip().casefold() == proyecto_nombre.casefold()
        ]
        if len(coincidencias_nombre) == 1:
            equipo = coincidencias_nombre[0]

    if equipo is None:
        equipo = Equipo(
            comision_id=comision_id,
            nombre_proyecto=proyecto_nombre,
            url_github=url_github,
        )
        db.add(equipo)
        return equipo, True, False

    actualizado = False
    if equipo.nombre_proyecto != proyecto_nombre:
        equipo.nombre_proyecto = proyecto_nombre
        actualizado = True
    if equipo.url_github != url_github:
        equipo.url_github = url_github
        actualizado = True
    return equipo, False, actualizado


def _buscar_estudiante(db: Session, dni: str, email: str | None) -> Estudiante | None:
    estudiante = db.query(Estudiante).filter(Estudiante.dni == dni).first()
    if estudiante is None and email:
        estudiante = (
            db.query(Estudiante)
            .filter(func.lower(Estudiante.email) == email.casefold())
            .first()
        )
    return estudiante


def importar_archivo(db: Session, path: str | Path) -> dict[str, Any]:
    """Importa estudiantes y sus entidades relacionadas desde un archivo.

    Las filas se aíslan mediante savepoints: una fila inválida se informa en el
    resumen sin descartar las demás. Los cambios válidos se confirman juntos.
    """
    dataframe = _leer_archivo(path)
    dataframe.columns = [_normalizar_columna(columna) for columna in dataframe.columns]
    if dataframe.columns.duplicated().any():
        raise ValueError("El archivo contiene nombres de columnas duplicados")
    columnas_faltantes = sorted(_COLUMNAS_REQUERIDAS - set(dataframe.columns))
    if columnas_faltantes:
        raise ValueError(
            "Faltan columnas requeridas: " + ", ".join(columnas_faltantes)
        )

    resumen: dict[str, Any] = {
        "cohortes": {"creadas": 0, "actualizadas": 0},
        "equipos": {"creados": 0, "actualizados": 0},
        "estudiantes": {"creados": 0, "actualizados": 0},
        "filas_procesadas": 0,
        "filas_con_error": [],
    }

    for numero_fila, (_, fila) in enumerate(dataframe.iterrows(), start=2):
        cambios = {
            "cohorte_creada": False,
            "equipo_creado": False,
            "equipo_actualizado": False,
            "estudiante_creado": False,
            "estudiante_actualizado": False,
        }

        try:
            cohorte_anio = _entero(fila["cohorte"], "cohorte")
            comision_numero = _entero(fila["comision"], "comision")
            equipo_etiqueta = _texto(fila["equipo"])
            proyecto_nombre = _texto(fila["proyecto_nombre"])
            url_github = _texto(fila["url_github"])
            apellido_nombre = _texto(fila["apellido_nombre"])
            dni = _texto(fila["dni"])
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
                "proyecto_nombre": proyecto_nombre,
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

                comision = (
                    db.query(Comision)
                    .filter(
                        Comision.cohorte_id == cohorte.id,
                        Comision.numero == comision_numero,
                    )
                    .first()
                )
                if comision is None:
                    comision = Comision(
                        cohorte_id=cohorte.id,
                        numero=comision_numero,
                    )
                    db.add(comision)
                    db.flush()

                equipo, equipo_creado, equipo_actualizado = _obtener_equipo(
                    db,
                    comision.id,
                    proyecto_nombre,
                    url_github,
                )
                db.flush()
                cambios["equipo_creado"] = equipo_creado
                cambios["equipo_actualizado"] = equipo_actualizado

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
        db.commit()
    except Exception:
        db.rollback()
        raise
    return resumen