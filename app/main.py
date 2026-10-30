from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.database import init_db
from dotenv import load_dotenv
load_dotenv()  # Carga las variables de entorno desde el archivo .env

# Configuración inicial de la base de datos

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BD1 Seguimiento", lifespan=lifespan)


@app.get("/")
def root():
    return {"status": "ok"}