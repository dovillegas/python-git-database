from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.database import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="BD1 Seguimiento", lifespan=lifespan)


@app.get("/")
def root():
    return {"status": "ok"}