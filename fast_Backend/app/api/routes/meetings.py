from fastapi import APIRouter


router = APIRouter(prefix="/meetings", tags=["meetings"])

@router.post("/add")
async def add_meeting():
    pass

@router.post("/process")
async def process_meeting():
    pass


@router.get("")
async def list_meetings():
    pass


@router.get("/{meeting_id}")
async def get_meeting(meeting_id: str):
    pass


@router.get("/{meeting_id}/status")
async def get_meeting_status(meeting_id: str):
    pass


@router.delete("/{meeting_id}")
async def delete_meeting(meeting_id: str):
    pass
