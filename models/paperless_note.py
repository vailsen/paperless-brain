from pydantic import BaseModel
from datetime import datetime


class PaperlessNote(BaseModel):
    id: int
    note: str
    created: datetime
    user: dict
