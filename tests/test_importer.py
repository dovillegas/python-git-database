import csv

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Cohorte, Comision, Equipo, Estudiante
from app.services import pipeline_service
from app.services.importer import importar_archivo


@pytest.fixture

def importer_db(monkeypatch):
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = sessionmaker(bind=engine, autoflush=False)()
    monkeypatch.setattr(pipeline_service, "estado", lambda: {"activo": False})
    yield db
    db.close()
    engine.dispose()


def escribir_csv(tmp_path, filas):
    path = tmp_path / "estudiantes.csv"
    columnas = [
        "cohorte",
        "comision",
        "equipo",
        "proyecto_nombre",
        "url_github",
        "apellido_nombre",
        "dni",
        "email",
        "usuario_github",
    ]
    with path.open("w", encoding="utf-8", newline="") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=columnas)
        escritor.writeheader()
        escritor.writerows(filas)
    return path


def fila(**cambios):
    datos = {
        "cohorte": "2026",
        "comision": "1",
        "equipo": "Grupo A",
        "proyecto_nombre": "Proyecto A",
        "url_github": "https://github.com/acme/proyecto.git/",
        "apellido_nombre": "Pérez, Ana",
        "dni": "12345678",
        "email": "ana@example.com",
        "usuario_github": "anaperez",
    }
    datos.update(cambios)
    return datos


def crear_equipo(db, nombre="Grupo A", comision_numero=1, url="https://github.com/acme/proyecto"):
    cohorte = Cohorte(anio=2026)
    db.add(cohorte)
    db.flush()
    comision = Comision(cohorte_id=cohorte.id, numero=comision_numero)
    db.add(comision)
    db.flush()
    equipo = Equipo(
        comision_id=comision.id,
        nombre=nombre,
        nombre_proyecto="Proyecto A",
        url_github=url,
        estado_salud="ok",
    )
    db.add(equipo)
    db.commit()
    return cohorte, comision, equipo


def test_crea_comision_equipo_y_estudiante_normalizados_idempotentemente(importer_db, tmp_path):
    path = escribir_csv(tmp_path, [fila(comision="4")])

    primero = importar_archivo(importer_db, path)
    segundo = importar_archivo(importer_db, path)

    equipo = importer_db.query(Equipo).one()
    estudiante = importer_db.query(Estudiante).one()
    assert primero["cohortes"]["creadas"] == 1
    assert primero["equipos"]["creados"] == 1
    assert primero["estudiantes"]["creados"] == 1
    assert primero["estudiantes_nuevos"] == [{"equipo": "Grupo A", "nombre": "Pérez, Ana"}]
    assert equipo.url_github == "https://github.com/acme/proyecto"
    assert equipo.estado_salud == "sin_verificar"
    assert estudiante.equipo_id == equipo.id
    assert segundo["cohortes"] == {"creadas": 0, "actualizadas": 0}
    assert segundo["equipos"] == {"creados": 0, "actualizados": 0}
    assert segundo["estudiantes"] == {"creados": 0, "actualizados": 0}
    assert importer_db.query(Comision).count() == 1
    assert importer_db.query(Equipo).count() == 1
    assert importer_db.query(Estudiante).count() == 1


def test_busca_equipo_en_todas_las_comisiones_y_registra_conflicto(importer_db, tmp_path):
    _, comision, equipo = crear_equipo(importer_db, nombre="  gRuPo A ")
    path = escribir_csv(tmp_path, [fila(comision="2")])

    resultado = importar_archivo(importer_db, path)

    assert equipo.comision_id == comision.id
    assert importer_db.query(Equipo).count() == 1
    assert resultado["conflictos"][0] == {
        "tipo": "comision_distinta",
        "fila": 2,
        "equipo": "  gRuPo A ",
        "comision_actual": 1,
        "comision_archivo": 2,
    }
    assert importer_db.query(Comision).count() == 1
    assert importer_db.query(Estudiante).count() == 1


def test_archivo_manda_no_mueve_un_equipo_a_otra_comision(importer_db, tmp_path):
    _, comision, equipo = crear_equipo(importer_db)
    path = escribir_csv(tmp_path, [fila(comision="2")])

    resultado = importar_archivo(importer_db, path, modo="archivo_manda")

    assert equipo.comision_id == comision.id
    assert importer_db.query(Equipo).count() == 1
    assert resultado["conflictos"][0]["tipo"] == "comision_distinta"


def test_equipo_repetido_en_comisiones_distintas_rechaza_todas_las_filas(importer_db, tmp_path):
    path = escribir_csv(
        tmp_path,
        [fila(comision="1"), fila(comision="2", dni="87654321", apellido_nombre="Otra")],
    )

    resultado = importar_archivo(importer_db, path)

    assert resultado["filas_procesadas"] == 0
    assert [error["fila"] for error in resultado["filas_con_error"]] == [2, 3]
    assert all(
        error["error"] == "nombre de equipo repetido en comisiones distintas"
        for error in resultado["filas_con_error"]
    )
    assert importer_db.query(Cohorte).count() == 0


def test_completa_url_vacia_y_actualiza_estudiante_del_mismo_equipo(importer_db, tmp_path):
    _, comision, equipo = crear_equipo(importer_db, url="")
    estudiante = Estudiante(
        equipo_id=equipo.id,
        apellido_nombre="Nombre anterior",
        dni="12345678",
    )
    importer_db.add(estudiante)
    importer_db.commit()
    path = escribir_csv(tmp_path, [fila(equipo=" grupo a ")])

    resultado = importar_archivo(importer_db, path)

    importer_db.refresh(equipo)
    importer_db.refresh(estudiante)
    assert equipo.comision_id == comision.id
    assert equipo.url_github == "https://github.com/acme/proyecto"
    assert estudiante.apellido_nombre == "Pérez, Ana"
    assert estudiante.equipo_id == equipo.id
    assert resultado["equipos"]["actualizados"] == 1
    assert resultado["estudiantes"]["actualizados"] == 1


@pytest.mark.parametrize(
    ("modo", "url_esperada", "tipo_conflicto"),
    [
        ("conservar_panel", "https://github.com/otra/repo", "url_distinta"),
        ("archivo_manda", "https://github.com/acme/proyecto", None),
    ],
)
def test_url_diferente_se_conserva_o_actualiza_segun_modo(
    importer_db, tmp_path, modo, url_esperada, tipo_conflicto
):
    crear_equipo(importer_db, url="https://github.com/otra/repo")
    path = escribir_csv(tmp_path, [fila()])

    resultado = importar_archivo(importer_db, path, modo=modo)

    equipo = importer_db.query(Equipo).one()
    assert equipo.url_github == url_esperada
    if tipo_conflicto:
        assert resultado["conflictos"] == [
            {
                "tipo": tipo_conflicto,
                "equipo": "Grupo A",
                "actual": "https://github.com/otra/repo",
                "archivo": "https://github.com/acme/proyecto",
            }
        ]
    else:
        assert resultado["conflictos"] == []


def test_estudiante_en_otro_equipo_no_se_mueve_y_dni_se_mascara(importer_db, tmp_path):
    cohorte, comision, equipo_actual = crear_equipo(importer_db, nombre="Grupo actual")
    otro = Equipo(
        comision_id=comision.id,
        nombre="Grupo destino",
        url_github="https://github.com/acme/proyecto",
    )
    importer_db.add(otro)
    importer_db.flush()
    estudiante = Estudiante(
        equipo_id=equipo_actual.id,
        apellido_nombre="Nombre actual",
        dni="12345678",
        email="ana@example.com",
    )
    importer_db.add(estudiante)
    importer_db.commit()
    path = escribir_csv(tmp_path, [fila(equipo="Grupo destino", url_github=otro.url_github)])

    resultado = importar_archivo(importer_db, path)

    importer_db.refresh(estudiante)
    conflicto = next(c for c in resultado["conflictos"] if c["tipo"] == "estudiante_en_otro_equipo")
    assert estudiante.equipo_id == equipo_actual.id
    assert estudiante.apellido_nombre == "Nombre actual"
    assert conflicto == {
        "tipo": "estudiante_en_otro_equipo",
        "fila": 2,
        "dni_parcial": "******78",
        "equipo_actual": "Grupo actual",
        "equipo_archivo": "Grupo destino",
    }
    assert "12345678" not in repr(conflicto)


def test_archivo_manda_mueve_estudiante_y_actualiza_sus_datos(importer_db, tmp_path):
    cohorte, comision, equipo_actual = crear_equipo(importer_db, nombre="Grupo actual")
    destino = Equipo(
        comision_id=comision.id,
        nombre="Grupo destino",
        url_github="https://github.com/acme/proyecto",
    )
    importer_db.add(destino)
    importer_db.flush()
    estudiante = Estudiante(
        equipo_id=equipo_actual.id,
        apellido_nombre="Nombre anterior",
        dni="12345678",
    )
    importer_db.add(estudiante)
    importer_db.commit()
    path = escribir_csv(tmp_path, [fila(equipo="Grupo destino", url_github=destino.url_github)])

    resultado = importar_archivo(importer_db, path, modo="archivo_manda")

    importer_db.refresh(estudiante)
    assert estudiante.equipo_id == destino.id
    assert estudiante.apellido_nombre == "Pérez, Ana"
    assert resultado["conflictos"] == []
    assert resultado["estudiantes"]["actualizados"] == 1


def test_previsualizar_devuelve_conteos_y_revierte_todo(importer_db, tmp_path):
    path = escribir_csv(tmp_path, [fila()])

    resultado = importar_archivo(importer_db, path, solo_previsualizar=True)

    assert resultado["cohortes"]["creadas"] == 1
    assert resultado["equipos"]["creados"] == 1
    assert resultado["estudiantes"]["creados"] == 1
    assert resultado["filas_procesadas"] == 1
    assert importer_db.query(Cohorte).count() == 0
    assert importer_db.query(Comision).count() == 0
    assert importer_db.query(Equipo).count() == 0
    assert importer_db.query(Estudiante).count() == 0


def test_pipeline_activo_rechaza_importacion_sin_escrituras(importer_db, tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline_service, "estado", lambda: {"activo": True})
    path = escribir_csv(tmp_path, [fila()])

    resultado = importar_archivo(importer_db, path)

    assert resultado["filas_procesadas"] == 0
    assert resultado["filas_con_error"][0]["fila"] is None
    assert "sincronización activa" in resultado["filas_con_error"][0]["error"]
    assert importer_db.query(Cohorte).count() == 0


def test_fila_invalida_se_informa_sin_lanzar_excepcion(importer_db, tmp_path):
    path = escribir_csv(tmp_path, [fila(comision="no-numérica")])

    resultado = importar_archivo(importer_db, path)

    assert resultado["filas_procesadas"] == 0
    assert resultado["filas_con_error"] == [
        {"fila": 2, "error": "'comision' debe ser un número entero"}
    ]
