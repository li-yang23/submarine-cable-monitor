from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional, Any
import json


class EventType(str, Enum):
    """Types of submarine cable events."""
    FAULT = "fault"
    OUTAGE = "outage"
    REPAIR = "repair"
    MAINTENANCE = "maintenance"
    NEWS = "news"
    UPDATE = "update"


class EventStatus(str, Enum):
    """Status of an event."""
    REPORTED = "reported"
    INVESTIGATING = "investigating"
    REPAIRING = "repairing"
    RESOLVED = "resolved"
    UNKNOWN = "unknown"


@dataclass
class Event:
    """Represents a submarine cable event."""
    source: str
    event_type: EventType
    cable_name: Optional[str] = None
    location: Optional[str] = None
    status: EventStatus = EventStatus.UNKNOWN
    reported_at: Optional[datetime] = None
    resolved_at: Optional[datetime] = None
    description: Optional[str] = None
    url: Optional[str] = None
    raw_data: Optional[dict[str, Any]] = None
    id: Optional[int] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary."""
        return {
            "id": self.id,
            "source": self.source,
            "event_type": self.event_type.value if isinstance(self.event_type, Enum) else self.event_type,
            "cable_name": self.cable_name,
            "location": self.location,
            "status": self.status.value if isinstance(self.status, Enum) else self.status,
            "reported_at": self.reported_at.isoformat() if self.reported_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "description": self.description,
            "url": self.url,
            "raw_data": self.raw_data,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat()
        }

    def to_json(self) -> str:
        """Convert event to JSON string."""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        """Create event from dictionary."""
        reported_at = None
        if data.get("reported_at"):
            if isinstance(data["reported_at"], str):
                reported_at = datetime.fromisoformat(data["reported_at"])
            else:
                reported_at = data["reported_at"]

        resolved_at = None
        if data.get("resolved_at"):
            if isinstance(data["resolved_at"], str):
                resolved_at = datetime.fromisoformat(data["resolved_at"])
            else:
                resolved_at = data["resolved_at"]

        created_at = datetime.utcnow()
        if data.get("created_at"):
            if isinstance(data["created_at"], str):
                created_at = datetime.fromisoformat(data["created_at"])
            else:
                created_at = data["created_at"]

        updated_at = datetime.utcnow()
        if data.get("updated_at"):
            if isinstance(data["updated_at"], str):
                updated_at = datetime.fromisoformat(data["updated_at"])
            else:
                updated_at = data["updated_at"]

        return cls(
            id=data.get("id"),
            source=data["source"],
            event_type=EventType(data["event_type"]) if isinstance(data["event_type"], str) else data["event_type"],
            cable_name=data.get("cable_name"),
            location=data.get("location"),
            status=EventStatus(data["status"]) if isinstance(data["status"], str) else data.get("status", EventStatus.UNKNOWN),
            reported_at=reported_at,
            resolved_at=resolved_at,
            description=data.get("description"),
            url=data.get("url"),
            raw_data=data.get("raw_data"),
            created_at=created_at,
            updated_at=updated_at
        )
