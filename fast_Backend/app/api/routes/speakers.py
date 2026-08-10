from fastapi import APIRouter


router = APIRouter(prefix="/speakers", tags=["speakers"])


@router.post("/register")
async def register_speaker():
    pass


@router.get("")
async def list_speakers():
    pass


@router.get("/{speaker_id}")
async def get_speaker(speaker_id: str):
    pass


@router.patch("/{speaker_id}")
async def update_speaker(speaker_id: str):
    pass


@router.delete("/{speaker_id}")
async def delete_speaker(speaker_id: str):
    pass


@router.post("/{speaker_id}/voice-samples")
async def add_voice_sample(speaker_id: str):
    pass
