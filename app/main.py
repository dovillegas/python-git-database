from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from sqlalchemy import func

from app.database import SessionLocal, init_db
from app.models import AutorGit
from app.routers.autores import router as autores_router, templates as autores_templates
from app.routers.descargas import router as descargas_router, templates as descargas_templates
from app.routers.importar import router as importar_router, templates as importar_templates
from app.routers.panel import router as panel_router, templates as panel_templates
from app.routers.sincronizacion import router as sincronizacion_router, templates as sincronizacion_templates
from app.routers.evaluacion import router as evaluacion_router, templates as evaluacion_templates
from dotenv import load_dotenv
load_dotenv()  # Carga las variables de entorno desde el archivo .env

# Configuración inicial de la base de datos

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BD1 Seguimiento", lifespan=lifespan)


def _autores_template_context(request: Request) -> dict[str, int]:
    return {"autores_pendientes": getattr(request.state, "autores_pendientes", 0)}


for templates in (
    autores_templates,
    descargas_templates,
    importar_templates,
    panel_templates,
    sincronizacion_templates,
    evaluacion_templates,
):
    templates.context_processors.append(_autores_template_context)


@app.middleware("http")
async def autores_pendientes_context(request: Request, call_next):
    db = SessionLocal()
    try:
        request.state.autores_pendientes = (
            db.query(func.count(AutorGit.id))
            .filter(AutorGit.estudiante_id.is_(None), AutorGit.ignorado.is_(False))
            .scalar()
            or 0
        )
    except Exception:
        request.state.autores_pendientes = 0
    finally:
        db.close()
    return await call_next(request)


app.include_router(importar_router)
app.include_router(descargas_router)
app.include_router(sincronizacion_router)
app.include_router(panel_router)
app.include_router(autores_router)
app.include_router(evaluacion_router)