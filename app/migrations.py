"""Migraciones SQLite pequeñas necesarias para la evolución del esquema."""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Engine


def migrar_guias_evaluacion(engine: Engine) -> None:
    """Agrega columnas de guía a rúbricas y criterios existentes."""
    inspector = inspect(engine)
    columnas_por_tabla = {
        tabla: {columna["name"] for columna in inspector.get_columns(tabla)}
        for tabla in ("rubricas", "criterios_evaluacion")
        if inspector.has_table(tabla)
    }
    cambios = {
        "rubricas": {"guia": "TEXT"},
        "criterios_evaluacion": {
            "descripcion": "TEXT",
            "que_revisar": "TEXT",
            "evidencia_adecuado": "TEXT",
            "evidencia_solido": "TEXT",
            "evidencia_destacado": "TEXT",
            "ejemplo_observacion": "TEXT",
        },
    }
    with engine.begin() as connection:
        for tabla, columnas in cambios.items():
            existentes = columnas_por_tabla.get(tabla)
            if existentes is None:
                continue
            for nombre, tipo in columnas.items():
                if nombre not in existentes:
                    connection.exec_driver_sql(
                        f"ALTER TABLE {tabla} ADD COLUMN {nombre} {tipo}"
                    )


def migrar_equipos(engine: Engine) -> None:
    """Separa el nombre del equipo del nombre del proyecto en bases existentes."""
    migrar_guias_evaluacion(engine)
    inspector = inspect(engine)
    if not inspector.has_table("equipos"):
        return

    columnas = {columna["name"] for columna in inspector.get_columns("equipos")}
    if "nombre" in columnas:
        return

    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.commit()
        transaction = connection.begin()
        try:
            connection.exec_driver_sql(
                """
                CREATE TABLE equipos_nueva (
                    id INTEGER NOT NULL PRIMARY KEY,
                    comision_id INTEGER NOT NULL,
                    nombre VARCHAR(255) NOT NULL,
                    nombre_proyecto VARCHAR(255),
                    url_github VARCHAR(2048) NOT NULL,
                    estado_salud VARCHAR(100),
                    FOREIGN KEY(comision_id) REFERENCES comisiones (id)
                )
                """
            )
            connection.exec_driver_sql(
                """
                INSERT INTO equipos_nueva (
                    id, comision_id, nombre, nombre_proyecto, url_github, estado_salud
                )
                SELECT id, comision_id, nombre_proyecto, nombre_proyecto,
                       url_github, estado_salud
                FROM equipos
                """
            )
            connection.exec_driver_sql("DROP TABLE equipos")
            connection.exec_driver_sql("ALTER TABLE equipos_nueva RENAME TO equipos")
            transaction.commit()
        except Exception:
            transaction.rollback()
            raise
        finally:
            connection.exec_driver_sql("PRAGMA foreign_keys=ON")