from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api.router import api_router
from .database import close_database, create_database_tables


@asynccontextmanager
async def lifespan(_: FastAPI):
    await create_database_tables()
    yield
    await close_database()


app = FastAPI(
    title="Persian Meeting Audio Processing API",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router)


@app.get("/", tags=["root"])
async def root():
    pass
