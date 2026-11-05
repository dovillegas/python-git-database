from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import DATA_DIR, get_db
from app.services import evaluacion_service as servicio

router = APIRouter(prefix="/evaluacion", tags=["evaluación"])
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
MAX_CSV_BYTES = 5 * 1024 * 1024
ESTADOS = {"aprobado", "no_aprobado", "parcial", "sin_evaluar"}
ETIQUETAS_NIVEL = {
    0: "No Evidencia",
    1: "Insuficiente",
    2: "Básico",
    3: "Adecuado",
    4: "Sólido",
    5: "Destacado",
}


def _pagina(request: Request, nombre: str, **contexto):
    return templates.TemplateResponse(
        request=request,
        name=nombre,
        context={"request": request, **contexto},
    )


def _numero_filtro(value: int | None, choices: list[dict], key: str = "id") -> int | None:
    if value is None or value < 1:
        return None
    return value if any(item.get(key) == value for item in choices) else None


def _resumen_contexto(resultado: dict) -> dict:
    return {
        "resultado": resultado,
        "etiquetas_nivel": ETIQUETAS_NIVEL,
    }


@router.get("", name="evaluacion_dashboard")
def dashboard(
    request: Request,
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    estado: str | None = None,
    orden: str = "total",
    direccion: str = "desc",
    db: Session = Depends(get_db),
):
    filtros = servicio.listar_filtros(db, cohorte_id)
    cohorte_id = _numero_filtro(cohorte_id, filtros["cohortes"])
    filtros = servicio.listar_filtros(db, cohorte_id)
    comision_id = _numero_filtro(comision_id, filtros["comisiones"])
    filas = servicio.evaluar_todos(db, cohorte_id, comision_id)
    contadores = {clave: sum(fila["estado"] == clave for fila in filas) for clave in ESTADOS}
    if estado in ESTADOS:
        filas = [fila for fila in filas if fila["estado"] == estado]
    if orden == "estado":
        orden_estado = {"no_aprobado": 0, "parcial": 1, "sin_evaluar": 2, "aprobado": 3}
        filas.sort(key=lambda fila: (orden_estado.get(fila["estado"], 4), fila["nombre"].casefold()), reverse=direccion == "desc")
    else:
        filas.sort(
            key=lambda fila: (fila["pct_total"] is not None, fila["pct_total"] or 0, fila["nombre"].casefold()),
            reverse=direccion != "asc",
        )
    rubricas = servicio.listar_rubricas(db)
    plantilla = "_evaluacion_dashboard_content.html" if request.headers.get("HX-Request", "").lower() == "true" else "evaluacion.html"
    return _pagina(
        request,
        plantilla,
        filas=filas,
        rubricas=rubricas,
        contadores=contadores,
        estado=estado if estado in ESTADOS else "",
        orden=orden if orden in {"total", "estado"} else "total",
        direccion=direccion if direccion in {"asc", "desc"} else "desc",
        cohorte_id=cohorte_id,
        comision_id=comision_id,
        etiquetas_nivel=ETIQUETAS_NIVEL,
        **filtros,
    )


@router.get("/rubricas", name="evaluacion_rubricas")
def rubricas(request: Request, db: Session = Depends(get_db)):
    definiciones = servicio.listar_rubricas(db)
    umbrales = servicio.obtener_umbrales(db)
    return _pagina(
        request,
        "evaluacion_rubricas.html",
        rubricas=definiciones,
        umbrales=umbrales,
        suma_total=sum(float(criterio["peso"] or 0) for rubrica in definiciones for criterio in rubrica["criterios"] if criterio["activo"]),
        impacto=None,
        mensaje=None,
        error=None,
    )


@router.post("/rubricas/pesos", name="evaluacion_previsualizar_pesos")
async def previsualizar_pesos(request: Request, db: Session = Depends(get_db)):
    formulario = await request.form()
    pesos: dict[int, str] = {}
    for llave, valor in formulario.multi_items():
        if llave.startswith("peso_"):
            try:
                pesos[int(llave.removeprefix("peso_"))] = str(valor)
            except ValueError:
                continue
    impacto = servicio.impacto_pesos(db, pesos)
    definiciones = servicio.listar_rubricas(db)
    umbrales = servicio.obtener_umbrales(db)
    ids_activos = {
        criterio["id"]
        for rubrica in definiciones
        for criterio in rubrica["criterios"]
        if criterio["activo"]
    }
    return _pagina(
        request,
        "evaluacion_rubricas.html",
        rubricas=definiciones,
        umbrales=umbrales,
        suma_total=sum(float(valor or 0) for criterio_id, valor in pesos.items() if criterio_id in ids_activos),
        impacto=impacto,
        pesos_propuestos=pesos,
        mensaje=None,
        error=None if impacto.get("ok") else impacto.get("mensaje"),
    )


@router.post("/rubricas/pesos/guardar", name="evaluacion_guardar_pesos")
async def guardar_pesos(request: Request, db: Session = Depends(get_db)):
    formulario = await request.form()
    if formulario.get("confirmar") != "si":
        raise HTTPException(status_code=400, detail="Se requiere confirmar el impacto de los pesos.")
    pesos: dict[int, str] = {}
    for llave, valor in formulario.multi_items():
        if llave.startswith("peso_"):
            try:
                pesos[int(llave.removeprefix("peso_"))] = str(valor)
            except ValueError:
                continue
    resultado = servicio.guardar_pesos(db, pesos)
    return _pagina(
        request,
        "evaluacion_rubricas.html",
        rubricas=servicio.listar_rubricas(db),
        umbrales=servicio.obtener_umbrales(db),
        suma_total=sum(float(item["peso"] or 0) for rubrica in servicio.listar_rubricas(db) for item in rubrica["criterios"] if item["activo"]),
        impacto=None,
        mensaje=resultado.get("mensaje") if resultado.get("ok") else None,
        error=None if resultado.get("ok") else resultado.get("mensaje"),
    )


@router.post("/rubricas/criterio", name="evaluacion_crear_criterio")
def crear_criterio(
    request: Request,
    rubrica_id: int = Form(...),
    codigo: str = Form(...),
    nombre: str = Form(...),
    peso: float = Form(...),
    descripcion: str | None = Form(default=None),
    que_revisar: str | None = Form(default=None),
    evidencia_adecuado: str | None = Form(default=None),
    evidencia_solido: str | None = Form(default=None),
    evidencia_destacado: str | None = Form(default=None),
    ejemplo_observacion: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    resultado = servicio.crear_criterio(
        db, rubrica_id, codigo, nombre, peso,
        descripcion=descripcion,
        que_revisar=que_revisar,
        evidencia_adecuado=evidencia_adecuado,
        evidencia_solido=evidencia_solido,
        evidencia_destacado=evidencia_destacado,
        ejemplo_observacion=ejemplo_observacion,
    )
    definiciones = servicio.listar_rubricas(db)
    return _pagina(
        request, "evaluacion_rubricas.html", rubricas=definiciones,
        umbrales=servicio.obtener_umbrales(db),
        suma_total=sum(float(c["peso"] or 0) for r in definiciones for c in r["criterios"] if c["activo"]),
        impacto=None, mensaje=resultado.get("mensaje") if resultado.get("ok") else None,
        error=None if resultado.get("ok") else resultado.get("mensaje"),
    )


@router.post("/rubricas/criterio/{criterio_id}", name="evaluacion_editar_criterio")
def editar_criterio(
    request: Request,
    criterio_id: int,
    descripcion: str | None = Form(default=None),
    que_revisar: str | None = Form(default=None),
    evidencia_adecuado: str | None = Form(default=None),
    evidencia_solido: str | None = Form(default=None),
    evidencia_destacado: str | None = Form(default=None),
    ejemplo_observacion: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    resultado = servicio.editar_criterio(
        db,
        criterio_id,
        descripcion=descripcion,
        que_revisar=que_revisar,
        evidencia_adecuado=evidencia_adecuado,
        evidencia_solido=evidencia_solido,
        evidencia_destacado=evidencia_destacado,
        ejemplo_observacion=ejemplo_observacion,
    )
    definiciones = servicio.listar_rubricas(db)
    return _pagina(
        request, "evaluacion_rubricas.html", rubricas=definiciones,
        umbrales=servicio.obtener_umbrales(db),
        suma_total=sum(float(c["peso"] or 0) for r in definiciones for c in r["criterios"] if c["activo"]),
        impacto=None, mensaje=resultado.get("mensaje") if resultado.get("ok") else None,
        error=None if resultado.get("ok") else resultado.get("mensaje"),
    )


@router.post("/rubricas/{rubrica_id}/guia", name="evaluacion_guardar_guia_rubrica")
def guardar_guia_rubrica(
    request: Request,
    rubrica_id: int,
    guia: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    resultado = servicio.guardar_guia_rubrica(db, rubrica_id, guia)
    error = resultado.get("mensaje") if isinstance(resultado, dict) and resultado.get("ok") is False else None
    definiciones = servicio.listar_rubricas(db)
    return _pagina(
        request, "evaluacion_rubricas.html", rubricas=definiciones,
        umbrales=servicio.obtener_umbrales(db),
        suma_total=sum(float(c["peso"] or 0) for r in definiciones for c in r["criterios"] if c["activo"]),
        impacto=None, mensaje="Guía de rúbrica guardada." if error is None else None,
        error=error,
    )


@router.post("/rubricas/criterio/{criterio_id}/activo", name="evaluacion_activar_criterio")
def activar_criterio(
    request: Request,
    criterio_id: int,
    activo: bool = Form(default=False),
    db: Session = Depends(get_db),
):
    resultado = servicio.activar_criterio(db, criterio_id, activo)
    definiciones = servicio.listar_rubricas(db)
    return _pagina(
        request, "evaluacion_rubricas.html", rubricas=definiciones,
        umbrales=servicio.obtener_umbrales(db),
        suma_total=sum(float(c["peso"] or 0) for r in definiciones for c in r["criterios"] if c["activo"]),
        impacto=None, mensaje=resultado.get("mensaje") if resultado.get("ok") else None,
        error=None if resultado.get("ok") else resultado.get("mensaje"),
    )


@router.post("/rubricas/umbrales", name="evaluacion_guardar_umbrales")
def guardar_umbrales(
    request: Request,
    total_pct: float = Form(...),
    rubrica_pct: float = Form(...),
    db: Session = Depends(get_db),
):
    resultado = servicio.guardar_umbrales(db, total_pct, rubrica_pct)
    definiciones = servicio.listar_rubricas(db)
    return _pagina(
        request, "evaluacion_rubricas.html", rubricas=definiciones,
        umbrales=servicio.obtener_umbrales(db),
        suma_total=sum(float(c["peso"] or 0) for r in definiciones for c in r["criterios"] if c["activo"]),
        impacto=None, mensaje=resultado.get("mensaje") if resultado.get("ok") else None,
        error=None if resultado.get("ok") else resultado.get("mensaje"),
    )


@router.get("/plantilla.csv", name="evaluacion_plantilla_csv")
def descargar_plantilla(
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    db: Session = Depends(get_db),
):
    contenido = servicio.obtener_plantilla_csv(db, cohorte_id, comision_id)
    return Response(content="\ufeff" + contenido, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="plantilla_evaluacion.csv"'})


@router.get("/exportar.csv", name="evaluacion_exportar_csv")
def descargar_exportacion(
    cohorte_id: int | None = None,
    comision_id: int | None = None,
    estado: str | None = None,
    db: Session = Depends(get_db),
):
    contenido = servicio.exportar_evaluaciones_csv(db, cohorte_id, comision_id, estado if estado in ESTADOS else None)
    return Response(content="\ufeff" + contenido, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="evaluaciones.csv"'})


@router.get("/importar", name="evaluacion_importar")
def pagina_importar(request: Request):
    return _pagina(request, "evaluacion_importar.html", vista_previa=None, resumen=None, token=None, error=None)


@router.post("/importar", name="evaluacion_procesar_csv")
async def procesar_csv(
    request: Request,
    archivo: UploadFile | None = File(default=None),
    token: str = Form(default=""),
    modo: str = Form(default="sobrescribir"),
    db: Session = Depends(get_db),
):
    if token:
        if len(token) != 32 or any(caracter not in "0123456789abcdef" for caracter in token):
            raise HTTPException(status_code=400, detail="Identificador de importación inválido.")
        ruta = DATA_DIR / "uploads" / f"evaluacion_{token}.csv"
        if not ruta.is_file():
            return _pagina(request, "evaluacion_importar.html", vista_previa=None, resumen=None, token=None, error="La previsualización expiró. Volvé a subir el CSV.")
        contenido = ruta.read_bytes()
        resultado = servicio.aplicar_importacion_csv(db, contenido, modo)
        ruta.unlink(missing_ok=True)
        return _pagina(request, "evaluacion_importar.html", vista_previa=None, resumen=resultado, token=None, error=None)

    if archivo is None:
        return _pagina(request, "evaluacion_importar.html", vista_previa=None, resumen=None, token=None, error="Seleccioná un archivo CSV.")
    if Path(archivo.filename or "").suffix.lower() != ".csv":
        await archivo.close()
        return _pagina(request, "evaluacion_importar.html", vista_previa=None, resumen=None, token=None, error="El formato admitido es CSV.")
    contenido = await archivo.read(MAX_CSV_BYTES + 1)
    await archivo.close()
    if len(contenido) > MAX_CSV_BYTES:
        return _pagina(request, "evaluacion_importar.html", vista_previa=None, resumen=None, token=None, error="El CSV supera el límite de 5 MB.")
    vista_previa = servicio.previsualizar_importacion_csv(db, contenido)
    token_nuevo = uuid4().hex
    carpeta = DATA_DIR / "uploads"
    carpeta.mkdir(parents=True, exist_ok=True)
    (carpeta / f"evaluacion_{token_nuevo}.csv").write_bytes(contenido)
    return _pagina(
        request, "evaluacion_importar.html", vista_previa=vista_previa,
        resumen=None, token=token_nuevo, error=None,
        filas_validas=sum(1 for fila in vista_previa.get("filas", []) if fila.get("equipo_id") and not fila.get("errores")),
    )


@router.get("/equipo/{equipo_id}", name="evaluacion_equipo")
def detalle_evaluacion(request: Request, equipo_id: int, db: Session = Depends(get_db)):
    resultado = servicio.evaluar_equipo(db, equipo_id)
    if not resultado.get("ok", True):
        raise HTTPException(status_code=404, detail=resultado.get("mensaje", "Equipo no encontrado."))
    rubricas = servicio.listar_rubricas(db)
    valoraciones = {item["criterio_id"]: item for item in resultado["criterios"]}
    return _pagina(
        request, "evaluacion_equipo.html", resultado=resultado,
        rubricas=rubricas, valoraciones=valoraciones, etiquetas_nivel=ETIQUETAS_NIVEL,
    )


@router.post("/equipo/{equipo_id}/valorar", name="evaluacion_valorar")
def valorar_criterio(
    request: Request,
    equipo_id: int,
    criterio_id: int = Form(...),
    nivel: str = Form(default=""),
    observacion: str = Form(default=""),
    db: Session = Depends(get_db),
):
    try:
        nivel_numero = int(nivel) if nivel.strip() else None
    except ValueError:
        raise HTTPException(status_code=422, detail="El nivel debe estar entre 0 y 5.") from None
    resultado = servicio.valorar(db, equipo_id, criterio_id, nivel_numero, observacion.strip() or None)
    if not resultado.get("ok"):
        raise HTTPException(status_code=422, detail=resultado.get("mensaje", "No se pudo guardar."))
    if request.headers.get("HX-Request", "").lower() == "true":
        return _pagina(request, "_evaluacion_resumen.html", **_resumen_contexto(resultado))
    return RedirectResponse(url=f"/evaluacion/equipo/{equipo_id}", status_code=303)
