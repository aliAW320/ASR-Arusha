from fastapi import APIRouter

from .routes import auth, health, meetings, speakers , history


api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(speakers.router)
api_router.include_router(meetings.router)
api_router.include_router(history.router)