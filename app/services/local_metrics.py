"""Cálculo de métricas de actividad a partir del historial Git local."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Iterable

from git import Repo
from git.exc import GitCommandError, InvalidGitRepositoryError, NoSuchPathError

from app.services.git_cloner import repo_path


_SEPARADOR_COMMIT = "\x1e"
_SEPARADOR_CAMPO = "\x1f"
_FORMATO_COMMIT = "%x1e%H%x1f%an%x1f%ae%x1f%aI"
_EMAIL_NOREPLY = re.compile(
    r"^(?:\d+\+)?([^@]+)@users\.noreply\.github\.com$", re.IGNORECASE
)


def _campo(estudiante: Any, nombre: str, defecto: Any = None) -> Any:
    if isinstance(estudiante, dict):
        return estudiante.get(nombre, defecto)
    return getattr(estudiante, nombre, defecto)


def _email_normalizado(email: Any) -> str:
    return str(email or "").strip().lower()


def _usuario_normalizado(usuario: Any) -> str:
    return str(usuario or "").strip().removeprefix("@").lower()


def _incluir_archivo(ruta: str) -> bool:
    ruta = ruta.replace("\\", "/")
    partes = PurePosixPath(ruta).parts
    nombre = partes[-1].lower() if partes else ""
    if any(parte.lower() in {"node_modules", ".venv"} for parte in partes):
        return False
    return not (nombre.endswith(".min.js") or nombre.endswith(".lock"))


def _fecha_commit(valor: str) -> datetime:
    fecha = datetime.fromisoformat(valor.replace("Z", "+00:00"))
    return fecha.replace(tzinfo=timezone.utc) if fecha.tzinfo is None else fecha


def _acumulador() -> dict[str, Any]:
    return {
        "commits": 0,
        "lineas_agregadas": 0,
        "lineas_eliminadas": 0,
        "archivos": set(),
        "dias_activos": set(),
        "fechas": [],
        "commits_por_semana": Counter(),
        "commits_por_hora": Counter(),
    }


def _resumen_acumulador(datos: dict[str, Any]) -> dict[str, Any]:
    fechas = datos["fechas"]
    primero = min(fechas) if fechas else None
    ultimo = max(fechas) if fechas else None
    return {
        "commits": datos["commits"],
        "lineas_agregadas": datos["lineas_agregadas"],
        "lineas_eliminadas": datos["lineas_eliminadas"],
        "archivos_tocados": len(datos["archivos"]),
        "dias_activos": len(datos["dias_activos"]),
        "primer_commit": primero.isoformat() if primero else None,
        "ultimo_commit": ultimo.isoformat() if ultimo else None,
        "commits_por_semana": dict(sorted(datos["commits_por_semana"].items())),
        "commits_por_hora": {
            str(hora): datos["commits_por_hora"].get(str(hora), 0)
            for hora in range(24)
        },
    }


def _leer_historial(repo: Repo) -> list[dict[str, Any]]:
    salida = repo.git.log(
        "--numstat",
        "--no-merges",
        f"--format={_FORMATO_COMMIT}",
    )
    historial: list[dict[str, Any]] = []

    for bloque in salida.split(_SEPARADOR_COMMIT):
        lineas = bloque.strip("\n").splitlines()
        if not lineas:
            continue
        campos = lineas[0].split(_SEPARADOR_CAMPO, 3)
        if len(campos) != 4:
            continue

        registro = {
            "hash": campos[0],
            "autor": campos[1],
            "email": campos[2],
            "fecha": _fecha_commit(campos[3]),
            "archivos": set(),
            "agregadas": 0,
            "eliminadas": 0,
        }
        for linea in lineas[1:]:
            partes = linea.split("\t", 2)
            if len(partes) != 3:
                continue
            agregadas, eliminadas, ruta = partes
            if not _incluir_archivo(ruta) or agregadas == "-" or eliminadas == "-":
                # Git numstat representa los archivos binarios con guiones.
                continue
            try:
                cantidad_agregadas = int(agregadas)
                cantidad_eliminadas = int(eliminadas)
            except ValueError:
                continue

            registro["archivos"].add(ruta)
            registro["agregadas"] += cantidad_agregadas
            registro["eliminadas"] += cantidad_eliminadas
        historial.append(registro)
    return historial


def _buscar_estudiante(
    email: str,
    autor: str,
    por_email: dict[str, int],
    por_usuario: dict[str, int],
) -> int | None:
    normalizado = _email_normalizado(email)
    if normalizado in por_email:
        return por_email[normalizado]

    coincidencia = _EMAIL_NOREPLY.fullmatch(normalizado)
    if coincidencia:
        indice = por_usuario.get(_usuario_normalizado(coincidencia.group(1)))
        if indice is not None:
            return indice
    return por_usuario.get(_usuario_normalizado(autor))


def _indice_alias(
    alias: dict[str, int | None],
    email: str,
    por_id: dict[int, int],
) -> tuple[bool, int | None]:
    """Devuelve si hay alias para el email y su índice (None si es ignorado)."""
    normalizado = _email_normalizado(email)
    if normalizado not in alias:
        return False, None
    id_estudiante = alias[normalizado]
    if id_estudiante is None:
        return True, None
    return True, por_id.get(id_estudiante)


def metricas_locales(
    cohorte: int | str,
    equipo_nombre: str,
    estudiantes: Iterable[Any],
    alias: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    """Devuelve métricas del repo local, separadas por equipo y estudiante.

    Los estudiantes pueden ser instancias de modelo o diccionarios con los
    campos ``email``, ``usuario_github`` y ``apellido_nombre``.
    """
    lista_estudiantes = list(estudiantes or [])
    alias_normalizado = {
        _email_normalizado(email): id_estudiante
        for email, id_estudiante in (alias or {}).items()
    }
    por_email: dict[str, int] = {}
    por_usuario: dict[str, int] = {}
    por_id: dict[Any, int] = {}
    for indice, estudiante in enumerate(lista_estudiantes):
        email = _email_normalizado(_campo(estudiante, "email"))
        usuario = _usuario_normalizado(_campo(estudiante, "usuario_github"))
        id_estudiante = _campo(estudiante, "id")
        if id_estudiante is not None:
            por_id.setdefault(id_estudiante, indice)
        if email:
            por_email.setdefault(email, indice)
        if usuario:
            por_usuario.setdefault(usuario, indice)
    for indice, estudiante in enumerate(lista_estudiantes):
        if _campo(estudiante, "id") is None:
            por_id.setdefault(indice, indice)

    acumulados_estudiantes = [_acumulador() for _ in lista_estudiantes]
    autores_sin_asignar: dict[str, dict[str, Any]] = {}
    autores_detectados: dict[str, dict[str, Any]] = {}
    commits_por_semana: Counter[str] = Counter()
    archivos_equipo: set[str] = set()
    total_agregadas = 0
    total_eliminadas = 0
    commits_ignorados = 0
    commits_por_hora_equipo: Counter[str] = Counter()
    error: str | None = None

    try:
        historial = _leer_historial(Repo(repo_path(cohorte, equipo_nombre)))
    except (
        GitCommandError,
        InvalidGitRepositoryError,
        NoSuchPathError,
        OSError,
    ) as exc:
        historial = []
        error = str(exc)

    for commit in historial:
        fecha: datetime = commit["fecha"]
        dia = fecha.date()
        anio_semana, numero_semana, _ = fecha.isocalendar()
        semana = f"{anio_semana}-W{numero_semana:02d}"
        hora = str(fecha.hour)
        commits_por_semana[semana] += 1
        commits_por_hora_equipo[hora] += 1
        archivos_equipo.update(commit["archivos"])
        total_agregadas += commit["agregadas"]
        total_eliminadas += commit["eliminadas"]

        hay_alias, indice = _indice_alias(
            alias_normalizado, commit["email"], por_id
        )
        if not hay_alias:
            indice = _buscar_estudiante(
                commit["email"], commit["autor"], por_email, por_usuario
            )
        ignorado = (
            hay_alias
            and alias_normalizado[_email_normalizado(commit["email"])] is None
        )
        email_autor = _email_normalizado(commit["email"])
        autor_datos = autores_detectados.setdefault(
            email_autor,
            {"email": email_autor, "nombres": set(), "commits": 0},
        )
        autor_datos["nombres"].add(commit["autor"])
        autor_datos["commits"] += 1
        if ignorado:
            autor_datos["ignorado"] = True
            autor_datos["estudiante_id"] = None
        else:
            autor_datos["ignorado"] = False
            autor_datos["estudiante_id"] = (
                _campo(lista_estudiantes[indice], "id")
                if indice is not None
                else None
            )
        if ignorado:
            commits_ignorados += 1
            continue
        if indice is None:
            email = _email_normalizado(commit["email"])
            destino = autores_sin_asignar.setdefault(
                email,
                {**_acumulador(), "nombres": set()},
            )
            destino["nombres"].add(commit["autor"])
        else:
            destino = acumulados_estudiantes[indice]
        destino["commits"] += 1
        destino["lineas_agregadas"] += commit["agregadas"]
        destino["lineas_eliminadas"] += commit["eliminadas"]
        destino["archivos"].update(commit["archivos"])
        destino["dias_activos"].add(dia)
        destino["fechas"].append(fecha)
        destino["commits_por_semana"][semana] += 1
        destino["commits_por_hora"][hora] += 1

    fechas = [commit["fecha"] for commit in historial]
    ultimo_commit = max(fechas) if fechas else None
    hoy = datetime.now(timezone.utc).date()
    resultado_estudiantes = []
    for estudiante, datos in zip(lista_estudiantes, acumulados_estudiantes):
        resultado_estudiantes.append(
            {
                "estudiante_id": _campo(estudiante, "id"),
                "estudiante": _campo(estudiante, "apellido_nombre"),
                "usuario_github": _campo(estudiante, "usuario_github"),
                **_resumen_acumulador(datos),
            }
        )

    resultado_autores = [
        {
            "autor": sorted(datos["nombres"], key=str.lower)[0],
            "nombres": sorted(datos["nombres"], key=str.lower),
            "email": email,
            **_resumen_acumulador(datos),
        }
        for email, datos in sorted(
            autores_sin_asignar.items(),
            key=lambda item: item[0],
        )
    ]
    resultado_autores_detectados = [
        {
            "email": email,
            "nombre_git": sorted(datos["nombres"], key=str.lower)[0],
            "commits": datos["commits"],
            "estudiante_id": datos.get("estudiante_id"),
            "ignorado": datos.get("ignorado", False),
        }
        for email, datos in sorted(autores_detectados.items())
    ]

    return {
        "equipo": {
            "commits": len(historial),
            "ultimo_commit": ultimo_commit.isoformat() if ultimo_commit else None,
            "dias_sin_actividad": (hoy - ultimo_commit.date()).days
            if ultimo_commit
            else None,
            "lineas_agregadas": total_agregadas,
            "lineas_eliminadas": total_eliminadas,
            "archivos": len(archivos_equipo),
            "commits_por_semana": dict(sorted(commits_por_semana.items())),
            "commits_por_hora": {
                str(hora): commits_por_hora_equipo.get(str(hora), 0)
                for hora in range(24)
            },
            "commits_ignorados": commits_ignorados,
        },
        "estudiantes": resultado_estudiantes,
        "autores_sin_asignar": resultado_autores,
        "autores": resultado_autores_detectados,
        "error": error,
    }