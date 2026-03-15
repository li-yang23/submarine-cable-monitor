import os
import sqlite3
import json
from datetime import datetime, timedelta
from typing import Optional, List, Any, Dict
from contextlib import contextmanager

from .models import Event, EventType, EventStatus


class Database:
    """SQLite database wrapper for storing submarine cable events."""

    def __init__(self, db_path: str):
        """
        Initialize the database.

        Args:
            db_path: Path to the SQLite database file
        """
        self.db_path = db_path
        self._ensure_db_directory()

    def _ensure_db_directory(self) -> None:
        """Ensure the database directory exists."""
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

    @contextmanager
    def _get_connection(self):
        """Get a database connection."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        """Create database tables if they don't exist."""
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    cable_name TEXT,
                    location TEXT,
                    status TEXT NOT NULL,
                    reported_at TIMESTAMP,
                    resolved_at TIMESTAMP,
                    description TEXT,
                    url TEXT,
                    raw_data TEXT,
                    created_at TIMESTAMP NOT NULL,
                    updated_at TIMESTAMP NOT NULL,
                    UNIQUE(source, url) ON CONFLICT IGNORE
                )
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_source ON events(source)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_event_type ON events(event_type)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_status ON events(status)
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_reported_at ON events(reported_at)
            """)

    def insert_event(self, event: Event) -> int:
        """
        Insert a new event.

        Args:
            event: Event to insert

        Returns:
            ID of the inserted event
        """
        raw_data_json = json.dumps(event.raw_data, ensure_ascii=False) if event.raw_data else None

        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT OR IGNORE INTO events (
                    source, event_type, cable_name, location, status,
                    reported_at, resolved_at, description, url, raw_data,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                event.source,
                event.event_type.value if isinstance(event.event_type, EventType) else event.event_type,
                event.cable_name,
                event.location,
                event.status.value if isinstance(event.status, EventStatus) else event.status,
                event.reported_at.isoformat() if event.reported_at else None,
                event.resolved_at.isoformat() if event.resolved_at else None,
                event.description,
                event.url,
                raw_data_json,
                event.created_at.isoformat(),
                event.updated_at.isoformat()
            ))
            if cursor.lastrowid:
                return cursor.lastrowid

            # If INSERT IGNORE did nothing, try to find existing row
            if event.url:
                cursor = conn.execute(
                    "SELECT id FROM events WHERE source = ? AND url = ?",
                    (event.source, event.url)
                )
                row = cursor.fetchone()
                if row:
                    return row["id"]

            return cursor.lastrowid or 0

    def insert_events(self, events: List[Event]) -> List[int]:
        """
        Insert multiple events.

        Args:
            events: List of events to insert

        Returns:
            List of inserted event IDs
        """
        return [self.insert_event(event) for event in events]

    def get_event(self, event_id: int) -> Optional[Event]:
        """
        Get an event by ID.

        Args:
            event_id: Event ID

        Returns:
            Event if found, None otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,))
            row = cursor.fetchone()
            return self._row_to_event(row) if row else None

    def get_events(
        self,
        source: Optional[str] = None,
        event_type: Optional[EventType] = None,
        status: Optional[EventStatus] = None,
        limit: Optional[int] = None,
        offset: int = 0
    ) -> List[Event]:
        """
        Get events with optional filters.

        Args:
            source: Filter by source
            event_type: Filter by event type
            status: Filter by status
            limit: Maximum number of events to return
            offset: Offset for pagination

        Returns:
            List of events
        """
        query = "SELECT * FROM events WHERE 1=1"
        params: List[Any] = []

        if source:
            query += " AND source = ?"
            params.append(source)

        if event_type:
            query += " AND event_type = ?"
            params.append(event_type.value)

        if status:
            query += " AND status = ?"
            params.append(status.value)

        query += " ORDER BY reported_at DESC, created_at DESC"

        if limit:
            query += " LIMIT ? OFFSET ?"
            params.extend([limit, offset])

        with self._get_connection() as conn:
            cursor = conn.execute(query, params)
            rows = cursor.fetchall()
            return [self._row_to_event(row) for row in rows]

    def get_all_events(self) -> List[Event]:
        """Get all events."""
        return self.get_events()

    def cleanup_old_events(self, retention_days: int = 730) -> int:
        """
        Delete events older than the retention period.

        Args:
            retention_days: Number of days to retain events

        Returns:
            Number of events deleted
        """
        cutoff = datetime.utcnow() - timedelta(days=retention_days)

        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM events WHERE created_at < ?",
                (cutoff.isoformat(),)
            )
            return cursor.rowcount

    def export_to_json(self, file_path: str) -> None:
        """
        Export all events to a JSON file.

        Args:
            file_path: Path to the output JSON file
        """
        events = self.get_all_events()
        data = [event.to_dict() for event in events]

        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def export_to_csv(self, file_path: str) -> None:
        """
        Export all events to a CSV file.

        Args:
            file_path: Path to the output CSV file
        """
        import csv

        events = self.get_all_events()

        directory = os.path.dirname(file_path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        if not events:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("")
            return

        fieldnames = [
            "id", "source", "event_type", "cable_name", "location", "status",
            "reported_at", "resolved_at", "description", "url", "created_at", "updated_at"
        ]

        with open(file_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for event in events:
                d = event.to_dict()
                writer.writerow({k: d.get(k) for k in fieldnames})

    def _row_to_event(self, row: sqlite3.Row) -> Event:
        """Convert a database row to an Event object."""
        raw_data = None
        if row["raw_data"]:
            try:
                raw_data = json.loads(row["raw_data"])
            except json.JSONDecodeError:
                raw_data = None

        return Event(
            id=row["id"],
            source=row["source"],
            event_type=EventType(row["event_type"]),
            cable_name=row["cable_name"],
            location=row["location"],
            status=EventStatus(row["status"]),
            reported_at=datetime.fromisoformat(row["reported_at"]) if row["reported_at"] else None,
            resolved_at=datetime.fromisoformat(row["resolved_at"]) if row["resolved_at"] else None,
            description=row["description"],
            url=row["url"],
            raw_data=raw_data,
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"])
        )
