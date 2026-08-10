from fastapi import APIRouter


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register")
async def register_user():
    pass


@router.post("/login")
async def login_user():
    pass
