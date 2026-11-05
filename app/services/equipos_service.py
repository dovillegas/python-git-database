"""Operaciones de equipos e integrantes, independientes de la capa web."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import (
    AutorGit,
    Cohorte,
    Comision,
    Equipo,
    Estudiante,
    MetricaEstudiante,
)
from app.services.git_cloner import REPOS_DIR, repo_path


def _pipeline_activo() -> bool:
    # pipeline_service conserva "running"; también se admite "activo" por compatibilidad.
    from app.services.pipeline_service import estado

    estado_actual = estado()
    return estado_actual.get("activo") is True or estado_actual.get("running") is True


def _error_bloqueado() -> dict[str, Any] | None:
    if _pipeline_activo():
        return {
            "ok": False,
            "mensaje": "No se puede modificar mientras hay una sincronización activa.",
        }
    return None


def _normalizar_nombre(valor: Any) -> str:
    return str(valor or "").strip()


def _normalizar_url(valor: Any) -> str:
    texto = str(valor or "").strip().rstrip("/")
    if texto.endswith(".git"):
        texto = texto[:-4].rstrip("/")

    partes = urlsplit(texto)
    segmentos = partes.path.strip("/").split("/") if partes.path.strip("/") else []
    if (
        partes.scheme.casefold() != "https"
        or (partes.hostname or "").casefold() != "github.com"
        or partes.username is not None
        or partes.password is not None
        or partes.port is not None
        or partes.query
        or partes.fragment
        or len(segmentos) != 2
        or not all(re.fullmatch(r"[A-Za-z0-9_.-]+", segmento) for segmento in segmentos)
    ):
        raise ValueError("La URL debe tener el formato https://github.com/owner/repo")
    return f"https://github.com/{segmentos[0]}/{segmentos[1]}"


def _normalizar_proyecto(valor: Any) -> str | None:
    texto = str(valor or "").strip()
    return None if not texto or texto == "-" else texto


def _normalizar_dni(valor: Any) -> str:
    texto = str(valor or "").strip()
    return re.sub(r"\.0+$", "", texto)


def _normalizar_usuario(valor: Any) -> str | None:
    texto = str(valor or "").strip().removeprefix("@").strip()
    return texto or None


def _normalizar_email(valor: Any) -> str | None:
    texto = str(valor or "").strip()
    return texto or None


def _nombre_duplicado(
    db: Session,
    cohorte_id: int,
    nombre: str,
    excluir_equipo_id: int | None = None,
) -> bool:
    equipos = (
        db.query(Equipo)
        .join(Comision, Equipo.comision_id == Comision.id)
        .filter(Comision.cohorte_id == cohorte_id)
        .all()
    )
    nombre_normalizado = nombre.casefold()
    return any(
        equipo.id != excluir_equipo_id
        and equipo.nombre.strip().casefold() == nombre_normalizado
        for equipo in equipos
    )


def _obtener_comision(db: Session, comision_id: int) -> Comision | None:
    return db.query(Comision).filter(Comision.id == comision_id).first()


def _agregar_integrante_sin_commit(
    db: Session,
    equipo_id: int,
    apellido_nombre: Any,
    dni: Any,
    email: Any = None,
    usuario_github: Any = None,
) -> dict[str, Any]:
    equipo = db.query(Equipo).filter(Equipo.id == equipo_id).first()
    if equipo is None:
        return {"ok": False, "mensaje": "El equipo indicado no existe."}

    nombre_limpio = _normalizar_nombre(apellido_nombre)
    dni_limpio = _normalizar_dni(dni)
    if not nombre_limpio or not dni_limpio:
        return {"ok": False, "mensaje": "Apellido y nombre y DNI son obligatorios."}

    existente = db.query(Estudiante).filter(Estudiante.dni == dni_limpio).first()
    if existente is not None:
        if existente.equipo_id == equipo.id:
            return {
                "ok": False,
                "mensaje": "Ya existe un integrante con ese DNI en este equipo.",
            }
        equipo_existente = (
            db.query(Equipo).filter(Equipo.id == existente.equipo_id).first()
        )
        return {
            "ok": False,
            "mensaje": "El DNI ya está registrado en otro equipo.",
            "conflicto": {
                "estudiante_id": existente.id,
                "equipo": equipo_existente.nombre if equipo_existente else None,
            },
        }

    estudiante = Estudiante(
        equipo_id=equipo.id,
        apellido_nombre=nombre_limpio,
        dni=dni_limpio,
        email=_normalizar_email(email),
        usuario_github=_normalizar_usuario(usuario_github),
    )
    db.add(estudiante)
    db.flush()
    return {"ok": True, "estudiante": estudiante}


def crear_equipo(
    db: Session,
    cohorte_id: int,
    comision_id: int,
    nombre: str,
    url_github: str,
    proyecto_nombre: str | None = None,
    integrantes: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    bloqueado = _error_bloqueado()
    if bloqueado:
        return bloqueado

    try:
        nombre_limpio = _normalizar_nombre(nombre)
        if not nombre_limpio:
            raise ValueError("El nombre del equipo es obligatorio.")
        url_limpia = _normalizar_url(url_github)
        comision = _obtener_comision(db, comision_id)
        if comision is None or comision.cohorte_id != cohorte_id:
            raise ValueError("La comisión no pertenece a la cohorte indicada.")
        if _nombre_duplicado(db, cohorte_id, nombre_limpio):
            raise ValueError("Ya existe un equipo con ese nombre en la cohorte.")
        if integrantes is not None and not isinstance(integrantes, list):
            raise ValueError("Los integrantes deben enviarse como una lista.")

        equipo = Equipo(
            comision_id=comision.id,
            nombre=nombre_limpio,
            nombre_proyecto=_normalizar_proyecto(proyecto_nombre),
            url_github=url_limpia,
            estado_salud="sin_verificar",
        )
        db.add(equipo)
        db.flush()

        for integrante in integrantes or []:
            if not isinstance(integrante, dict):
                raise ValueError("Cada integrante debe ser un objeto con sus datos.")
            resultado = _agregar_integrante_sin_commit(
                db,
                equipo.id,
                integrante.get("apellido_nombre"),
                integrante.get("dni"),
                integrante.get("email"),
                integrante.get("usuario_github"),
            )
            if not resultado["ok"]:
                raise ValueError(resultado["mensaje"])

        db.commit()
        return {
            "ok": True,
            "mensaje": "Equipo creado correctamente.",
            "equipo_id": equipo.id,
            "equipo": equipo,
            "requiere_calculo": bool(integrantes),
        }
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "mensaje": str(exc)}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo crear el equipo: {exc}"}


def _ruta_local_segura(cohorte: int | str, nombre: str) -> Path | None:
    ruta = repo_path(cohorte, nombre)
    raiz = REPOS_DIR.resolve()
    try:
        resuelta = ruta.resolve(strict=False)
        resuelta.relative_to(raiz)
    except (OSError, ValueError):
        return None
    if resuelta == raiz or not (ruta.exists() or ruta.is_symlink()):
        return None
    return ruta


def _preparar_eliminacion_local(
    cohorte: int | str, nombre: str
) -> tuple[Path, Path] | None:
    ruta = _ruta_local_segura(cohorte, nombre)
    if ruta is None:
        return None
    temporal = ruta.with_name(f".{ruta.name}.pendiente-{uuid4().hex}")
    temporal_resuelta = temporal.parent.resolve(strict=False) / temporal.name
    try:
        temporal_resuelta.relative_to(REPOS_DIR.resolve())
    except (OSError, ValueError):
        return None
    ruta.rename(temporal)
    return ruta, temporal


def _restaurar_copia_local(etapa: tuple[Path, Path] | None) -> None:
    if etapa is None:
        return
    original, temporal = etapa
    if temporal.exists() or temporal.is_symlink():
        if not (original.exists() or original.is_symlink()):
            temporal.rename(original)


def _eliminar_copia_temporal(etapa: tuple[Path, Path] | None) -> None:
    if etapa is None:
        return
    _, temporal = etapa
    if temporal.is_symlink() or temporal.is_file():
        temporal.unlink()
    elif temporal.exists():
        shutil.rmtree(temporal)


def editar_equipo(
    db: Session,
    equipo_id: int,
    nombre: str | None = None,
    url_github: str | None = None,
    proyecto_nombre: str | None = None,
    comision_id: int | None = None,
) -> dict[str, Any]:
    bloqueado = _error_bloqueado()
    if bloqueado:
        return bloqueado

    etapa_local: tuple[Path, Path] | None = None
    try:
        equipo = db.query(Equipo).filter(Equipo.id == equipo_id).first()
        if equipo is None:
            raise ValueError("El equipo indicado no existe.")

        comision_actual = _obtener_comision(db, equipo.comision_id)
        cohorte_actual_id = comision_actual.cohorte_id if comision_actual else None
        if cohorte_actual_id is None:
            raise ValueError("No se pudo determinar la cohorte actual del equipo.")

        nombre_nuevo = equipo.nombre if nombre is None else _normalizar_nombre(nombre)
        if not nombre_nuevo:
            raise ValueError("El nombre del equipo es obligatorio.")
        url_nueva = equipo.url_github if url_github is None else _normalizar_url(url_github)
        proyecto_nuevo = (
            equipo.nombre_proyecto
            if proyecto_nombre is None
            else _normalizar_proyecto(proyecto_nombre)
        )
        comision_nueva = (
            comision_actual
            if comision_id is None
            else _obtener_comision(db, comision_id)
        )
        if comision_nueva is None:
            raise ValueError("La comisión indicada no existe.")
        cohorte_nueva = db.query(Cohorte).filter(
            Cohorte.id == comision_nueva.cohorte_id
        ).first()
        if cohorte_nueva is None:
            raise ValueError("La cohorte de destino no existe.")
        if _nombre_duplicado(
            db,
            cohorte_nueva.id,
            nombre_nuevo,
            excluir_equipo_id=equipo.id,
        ):
            raise ValueError("Ya existe un equipo con ese nombre en la cohorte de destino.")

        cambio_nombre = nombre_nuevo != equipo.nombre
        cambio_url = url_nueva != equipo.url_github
        cambio_cohorte = cohorte_nueva.id != cohorte_actual_id
        cambio_comision = comision_nueva.id != equipo.comision_id
        cambio_proyecto = proyecto_nuevo != equipo.nombre_proyecto
        hubo_cambios = any(
            (cambio_nombre, cambio_url, cambio_cohorte, cambio_comision, cambio_proyecto)
        )
        if not hubo_cambios:
            db.commit()
            return {
                "ok": True,
                "mensaje": "sin cambios",
                "equipo_id": equipo.id,
                "requiere_descarga": False,
            }

        requiere_descarga = cambio_nombre or cambio_url or cambio_cohorte
        if requiere_descarga:
            cohorte_anio = comision_actual.cohorte.anio if comision_actual.cohorte else cohorte_actual_id
            etapa_local = _preparar_eliminacion_local(cohorte_anio, equipo.nombre)

        equipo.nombre = nombre_nuevo
        equipo.url_github = url_nueva
        equipo.nombre_proyecto = proyecto_nuevo
        equipo.comision_id = comision_nueva.id
        db.flush()
        db.commit()
    except ValueError as exc:
        db.rollback()
        try:
            _restaurar_copia_local(etapa_local)
        except OSError:
            pass
        return {"ok": False, "mensaje": str(exc)}
    except Exception as exc:
        db.rollback()
        try:
            _restaurar_copia_local(etapa_local)
        except OSError:
            pass
        return {"ok": False, "mensaje": f"No se pudo editar el equipo: {exc}"}

    mensaje = "Equipo actualizado correctamente."
    if requiere_descarga:
        try:
            _eliminar_copia_temporal(etapa_local)
        except OSError as exc:
            mensaje = f"Equipo actualizado; no se pudo limpiar la copia local antigua: {exc}"
    return {
        "ok": True,
        "mensaje": mensaje,
        "equipo_id": equipo_id,
        "requiere_descarga": requiere_descarga,
    }


def agregar_integrante(
    db: Session,
    equipo_id: int,
    apellido_nombre: str,
    dni: str,
    email: str | None,
    usuario_github: str | None,
) -> dict[str, Any]:
    bloqueado = _error_bloqueado()
    if bloqueado:
        return bloqueado
    try:
        resultado = _agregar_integrante_sin_commit(
            db, equipo_id, apellido_nombre, dni, email, usuario_github
        )
        if not resultado["ok"]:
            db.rollback()
            return resultado
        estudiante = resultado["estudiante"]
        db.commit()
        return {
            "ok": True,
            "mensaje": "Integrante agregado correctamente.",
            "estudiante_id": estudiante.id,
            "requiere_calculo": True,
        }
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo agregar el integrante: {exc}"}


def mover_integrante(
    db: Session, estudiante_id: int, equipo_id: int
) -> dict[str, Any]:
    bloqueado = _error_bloqueado()
    if bloqueado:
        return bloqueado
    try:
        estudiante = db.query(Estudiante).filter(Estudiante.id == estudiante_id).first()
        if estudiante is None:
            raise ValueError("El integrante indicado no existe.")
        equipo_destino = db.query(Equipo).filter(Equipo.id == equipo_id).first()
        if equipo_destino is None:
            raise ValueError("El equipo de destino no existe.")
        if estudiante.equipo_id == equipo_destino.id:
            db.commit()
            return {
                "ok": True,
                "mensaje": "sin cambios",
                "estudiante_id": estudiante.id,
                "requiere_calculo": False,
            }
        duplicado = (
            db.query(Estudiante)
            .filter(
                Estudiante.equipo_id == equipo_destino.id,
                Estudiante.dni == estudiante.dni,
                Estudiante.id != estudiante.id,
            )
            .first()
        )
        if duplicado is not None:
            raise ValueError("Ya existe un integrante con ese DNI en el equipo de destino.")

        db.query(AutorGit).filter(
            AutorGit.estudiante_id == estudiante.id,
            AutorGit.equipo_id == estudiante.equipo_id,
        ).update(
            {AutorGit.estudiante_id: None, AutorGit.ignorado: False},
            synchronize_session=False,
        )
        db.query(MetricaEstudiante).filter(
            MetricaEstudiante.estudiante_id == estudiante.id
        ).delete(synchronize_session=False)
        estudiante.equipo_id = equipo_destino.id
        db.commit()
        return {
            "ok": True,
            "mensaje": "Integrante trasladado correctamente.",
            "estudiante_id": estudiante.id,
            "requiere_calculo": True,
        }
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "mensaje": str(exc)}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo trasladar el integrante: {exc}"}


def editar_integrante(
    db: Session,
    estudiante_id: int,
    apellido_nombre: str | None = None,
    email: str | None = None,
    usuario_github: str | None = None,
    dni: str | None = None,
) -> dict[str, Any]:
    bloqueado = _error_bloqueado()
    if bloqueado:
        return bloqueado
    try:
        estudiante = db.query(Estudiante).filter(Estudiante.id == estudiante_id).first()
        if estudiante is None:
            raise ValueError("El integrante indicado no existe.")

        cambios: dict[str, Any] = {}
        if apellido_nombre is not None:
            nombre_limpio = _normalizar_nombre(apellido_nombre)
            if not nombre_limpio:
                raise ValueError("Apellido y nombre es obligatorio.")
            if nombre_limpio != estudiante.apellido_nombre:
                cambios["apellido_nombre"] = nombre_limpio
        if dni is not None:
            dni_limpio = _normalizar_dni(dni)
            if not dni_limpio:
                raise ValueError("El DNI es obligatorio.")
            duplicado = (
                db.query(Estudiante)
                .filter(Estudiante.dni == dni_limpio, Estudiante.id != estudiante.id)
                .first()
            )
            if duplicado is not None:
                equipo_duplicado = db.get(Equipo, duplicado.equipo_id)
                nombre_equipo = equipo_duplicado.nombre if equipo_duplicado else "otro equipo"
                raise ValueError(f"Ese DNI ya está en {nombre_equipo}.")
            if dni_limpio != estudiante.dni:
                cambios["dni"] = dni_limpio
        if email is not None:
            email_limpio = _normalizar_email(email)
            if email_limpio != estudiante.email:
                cambios["email"] = email_limpio
        if usuario_github is not None:
            usuario_limpio = _normalizar_usuario(usuario_github)
            if usuario_limpio != estudiante.usuario_github:
                cambios["usuario_github"] = usuario_limpio
        if not cambios:
            db.commit()
            return {
                "ok": True,
                "mensaje": "sin cambios",
                "estudiante_id": estudiante.id,
                "requiere_calculo": False,
            }

        for atributo, valor in cambios.items():
            setattr(estudiante, atributo, valor)
        db.commit()
        return {
            "ok": True,
            "mensaje": "Integrante actualizado correctamente.",
            "estudiante_id": estudiante.id,
            "requiere_calculo": True,
        }
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "mensaje": str(exc)}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo editar el integrante: {exc}"}


def quitar_integrante(db: Session, estudiante_id: int) -> dict[str, Any]:
    bloqueado = _error_bloqueado()
    if bloqueado:
        return bloqueado
    try:
        estudiante = db.query(Estudiante).filter(Estudiante.id == estudiante_id).first()
        if estudiante is None:
            raise ValueError("El integrante indicado no existe.")

        autores_pendientes = (
            db.query(AutorGit)
            .filter(
                AutorGit.estudiante_id == estudiante.id,
                AutorGit.commits > 0,
            )
            .count()
        )
        db.query(AutorGit).filter(AutorGit.estudiante_id == estudiante.id).update(
            {AutorGit.estudiante_id: None, AutorGit.ignorado: False},
            synchronize_session=False,
        )
        db.query(MetricaEstudiante).filter(
            MetricaEstudiante.estudiante_id == estudiante.id
        ).delete(synchronize_session=False)
        db.delete(estudiante)
        db.commit()
        return {
            "ok": True,
            "mensaje": "Integrante eliminado correctamente.",
            "autores_pendientes": autores_pendientes,
            "requiere_calculo": True,
        }
    except ValueError as exc:
        db.rollback()
        return {"ok": False, "mensaje": str(exc)}
    except Exception as exc:
        db.rollback()
        return {"ok": False, "mensaje": f"No se pudo quitar el integrante: {exc}"}
