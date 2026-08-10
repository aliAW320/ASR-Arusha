from fastapi import APIRouter


router = APIRouter(tags=["History"])

@router.get("/history")
async def get_history():
    pass

@router.delete("/history/{action_id}")
async def delete_history(action_id: str):
    pass
