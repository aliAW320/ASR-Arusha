from fastapi import FastAPI

from .api.router import api_router


app = FastAPI(
    title="Persian Meeting Audio Processing API",
    version="0.1.0",
)

app.include_router(api_router)


@app.get("/", tags=["root"])
async def root():
    pass
