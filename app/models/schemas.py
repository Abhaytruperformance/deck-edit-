from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel


class ClientCreate(BaseModel):
    name: str
    contact_email: str | None = None
    contact_phone: str | None = None
    notes: str | None = None


class ClientOut(ClientCreate):
    id: UUID
    created_at: datetime
    updated_at: datetime


DeliverableType = Literal["pptx", "xlsx", "animated_html"]
ProjectStatus = Literal["draft", "in_review", "published"]


class ProjectCreate(BaseModel):
    title: str
    deliverable_type: DeliverableType


class ProjectOut(BaseModel):
    id: UUID
    client_id: UUID
    title: str
    deliverable_type: DeliverableType
    status: ProjectStatus
    created_at: datetime
    updated_at: datetime
