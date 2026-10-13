"""Owner-scoped persistent short- and long-term memory, separate from place data."""

import os

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from src.schema.memory import (MemoryRecord, MemoryMessage, SessionSnapshot, TripState,
                               PreferenceChange, checked_identity, memory_text)
from src.utils.utils import PROJECT_ROOT


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS vistra_memory_sessions (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    version BIGINT NOT NULL DEFAULT 0,
    summary TEXT NOT NULL DEFAULT '',
    trip_state JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id)
);
CREATE TABLE IF NOT EXISTS vistra_memory_messages (
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    turn BIGINT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, session_id, turn, role),
    FOREIGN KEY (user_id, session_id) REFERENCES vistra_memory_sessions(user_id, session_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS vistra_long_term_memories (
    user_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('preference', 'trip')),
    memory_key TEXT NOT NULL,
    content TEXT NOT NULL,
    source_quote TEXT NOT NULL,
    source_session_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, kind, memory_key)
);
CREATE INDEX IF NOT EXISTS vistra_memories_recent_idx
    ON vistra_long_term_memories (user_id, kind, updated_at DESC);
"""


class MemoryConflict(RuntimeError):
    """Another request updated the session while this turn was being generated."""


class PostgresMemoryStore:
    def __init__(self, dsn=None, *, connection=None):
        self._owns_connection = connection is None
        self.connection = connection
        if connection is None:
            load_dotenv(PROJECT_ROOT / ".env", override=False)
            dsn = dsn or os.getenv("POSTGRES_DSN")
            options = {"autocommit": True, "row_factory": dict_row, "connect_timeout": 10}
            if dsn:
                self.connection = psycopg.connect(dsn, **options)
            else:
                password = os.getenv("POSTGRES_PASSWORD")
                if not password:
                    raise ValueError("Configure POSTGRES_DSN or POSTGRES_PASSWORD to use chat memory.")
                self.connection = psycopg.connect(host=os.getenv("POSTGRES_HOST", "127.0.0.1"),
                    port=os.getenv("POSTGRES_PORT", "5432"), dbname=os.getenv("POSTGRES_DB", "vistra"),
                    user=os.getenv("POSTGRES_USER", "vistra"), password=password, **options)

    def ensure_schema(self):
        with self.connection.transaction():
            self.connection.execute(SCHEMA_SQL, prepare=False)

    def load_session(self, user_id, session_id, *, message_limit=8):
        checked_identity(user_id, "user_id")
        checked_identity(session_id, "session_id")
        if type(message_limit) is not int or not 1 <= message_limit <= 20:
            raise ValueError("message_limit must be between 1 and 20.")
        with self.connection.transaction(), self.connection.cursor(row_factory=dict_row) as cursor:
            # Row lock makes the session state and recent message window a consistent snapshot.
            cursor.execute("SELECT * FROM vistra_memory_sessions WHERE user_id=%s AND session_id=%s FOR SHARE",
                           (user_id, session_id))
            row = cursor.fetchone()
            if row is None:
                return SessionSnapshot()
            cursor.execute("""SELECT role, content FROM vistra_memory_messages
                WHERE user_id=%s AND session_id=%s ORDER BY turn DESC,
                CASE role WHEN 'assistant' THEN 0 ELSE 1 END LIMIT %s""",
                (user_id, session_id, message_limit))
            messages = list(reversed(cursor.fetchall()))
            return SessionSnapshot(version=row["version"], summary=row["summary"],
                trip_state=TripState.model_validate(row["trip_state"]),
                recent_messages=[MemoryMessage(**item) for item in messages])

    def recall(self, user_id, query="", *, preference_limit=12, trip_limit=3):
        checked_identity(user_id, "user_id")
        for limit in (preference_limit, trip_limit):
            if type(limit) is not int or not 1 <= limit <= 20:
                raise ValueError("Memory recall limits must be between 1 and 20.")
        if query:
            query = memory_text(query, maximum=8000)
        with self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""SELECT kind, memory_key AS key, content, source_quote
                FROM vistra_long_term_memories WHERE user_id=%s AND kind='preference'
                ORDER BY updated_at DESC, memory_key LIMIT %s""", (user_id, preference_limit))
            records = cursor.fetchall()
            cursor.execute("""SELECT kind, memory_key AS key, content, source_quote
                FROM vistra_long_term_memories WHERE user_id=%s AND kind='trip'
                ORDER BY ts_rank(to_tsvector('simple', content), plainto_tsquery('simple', %s)) DESC,
                updated_at DESC, memory_key LIMIT %s""", (user_id, query, trip_limit))
            records += cursor.fetchall()
        return [MemoryRecord(**row) for row in records]

    @staticmethod
    def _change(cursor, user_id, session_id, change):
        if change.operation == "delete":
            cursor.execute("DELETE FROM vistra_long_term_memories WHERE user_id=%s AND kind='preference' AND memory_key=%s",
                           (user_id, change.key))
        else:
            cursor.execute("""INSERT INTO vistra_long_term_memories
                (user_id, kind, memory_key, content, source_quote, source_session_id)
                VALUES (%s, 'preference', %s, %s, %s, %s)
                ON CONFLICT (user_id, kind, memory_key) DO UPDATE SET
                content=EXCLUDED.content, source_quote=EXCLUDED.source_quote,
                source_session_id=EXCLUDED.source_session_id, updated_at=now()""",
                (user_id, change.key, change.source_quote, change.source_quote, session_id))

    def commit_turn(self, user_id, session_id, *, expected_version, user_message, answer,
                    summary, trip_state, preference_changes=()):
        checked_identity(user_id, "user_id")
        checked_identity(session_id, "session_id")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("expected_version must be a nonnegative integer.")
        user_message = memory_text(user_message, maximum=8000)
        answer = memory_text(answer, maximum=24000) if answer is not None else None
        summary = memory_text(summary, maximum=2000) if summary else ""
        state = TripState.model_validate(trip_state).model_dump()
        changes = [PreferenceChange.model_validate(item) for item in preference_changes]
        if len(changes) > 9 or len({item.key for item in changes}) != len(changes):
            raise ValueError("Preference changes must contain at most one operation per supported key.")
        for change in changes:
            change.source_quote = memory_text(change.source_quote, maximum=1000)
            if change.source_quote not in user_message:
                raise ValueError("Preference updates require an exact quote from the current user message.")
        with self.connection.transaction(), self.connection.cursor(row_factory=dict_row) as cursor:
            cursor.execute("""INSERT INTO vistra_memory_sessions (user_id, session_id)
                VALUES (%s, %s) ON CONFLICT DO NOTHING""", (user_id, session_id))
            cursor.execute("SELECT version FROM vistra_memory_sessions WHERE user_id=%s AND session_id=%s FOR UPDATE",
                           (user_id, session_id))
            if cursor.fetchone()["version"] != expected_version:
                raise MemoryConflict("Session changed during this turn; retry with its latest memory.")
            version = expected_version + 1
            cursor.execute("""UPDATE vistra_memory_sessions SET version=%s, summary=%s, trip_state=%s,
                updated_at=now() WHERE user_id=%s AND session_id=%s""",
                (version, summary, Jsonb(state), user_id, session_id))
            for role, content in (("user", user_message), ("assistant", answer)):
                if content is not None:
                    cursor.execute("""INSERT INTO vistra_memory_messages
                        (user_id, session_id, turn, role, content) VALUES (%s, %s, %s, %s, %s)""",
                        (user_id, session_id, version, role, content))
            for change in changes:
                self._change(cursor, user_id, session_id, change)
        return version

    def set_preference(self, user_id, key, source_quote):
        checked_identity(user_id, "user_id")
        change = PreferenceChange(operation="set", key=key,
                                  source_quote=memory_text(source_quote, maximum=1000))
        with self.connection.transaction(), self.connection.cursor() as cursor:
            self._change(cursor, user_id, None, change)

    def save_trip(self, user_id, key, content, source_quote, *, session_id=None):
        checked_identity(user_id, "user_id")
        checked_identity(key, "trip key")
        if session_id is not None:
            checked_identity(session_id, "session_id")
        record = MemoryRecord(kind="trip", key=key, content=memory_text(content),
                              source_quote=memory_text(source_quote, maximum=1000))
        with self.connection.transaction():
            self.connection.execute("""INSERT INTO vistra_long_term_memories
                (user_id, kind, memory_key, content, source_quote, source_session_id)
                VALUES (%s, 'trip', %s, %s, %s, %s)
                ON CONFLICT (user_id, kind, memory_key) DO UPDATE SET content=EXCLUDED.content,
                source_quote=EXCLUDED.source_quote, source_session_id=EXCLUDED.source_session_id, updated_at=now()""",
                (user_id, key, record.content, record.source_quote, session_id))

    def delete_memory(self, user_id, kind, key):
        checked_identity(user_id, "user_id")
        if kind not in {"preference", "trip"}:
            raise ValueError("Memory kind must be preference or trip.")
        checked_identity(key, "memory key")
        with self.connection.transaction():
            cursor = self.connection.execute("DELETE FROM vistra_long_term_memories WHERE user_id=%s AND kind=%s AND memory_key=%s",
                                             (user_id, kind, key))
            return cursor.rowcount > 0

    def clear_session(self, user_id, session_id):
        checked_identity(user_id, "user_id")
        checked_identity(session_id, "session_id")
        with self.connection.transaction():
            # Keep a monotonically increasing revision so an in-flight old turn
            # cannot repopulate memory after a reset.
            cursor = self.connection.execute("""UPDATE vistra_memory_sessions SET summary='',
                trip_state='{}'::jsonb, version=version+1, updated_at=now()
                WHERE user_id=%s AND session_id=%s""", (user_id, session_id))
            self.connection.execute("DELETE FROM vistra_memory_messages WHERE user_id=%s AND session_id=%s",
                                    (user_id, session_id))
            return cursor.rowcount > 0

    def delete_user_memory(self, user_id):
        checked_identity(user_id, "user_id")
        with self.connection.transaction():
            self.connection.execute("DELETE FROM vistra_long_term_memories WHERE user_id=%s", (user_id,))
            self.connection.execute("DELETE FROM vistra_memory_sessions WHERE user_id=%s", (user_id,))

    def close(self):
        if self._owns_connection and self.connection is not None:
            self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
