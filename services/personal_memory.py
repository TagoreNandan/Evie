"""
Persistent Personal Memory Subsystem for Evie Assistant (Step 10).

Provides privacy-first, local, restart-safe storage for explicit user facts,
preferences, routines, and project context.

Invariants:
1. Completely separate from short-term GoalMemory.
2. Independent of computer control execution.
3. Untrusted contextual data only — NEVER bypasses Safety Gate or creates UniversalAction.
4. Survives process restarts and session transitions.
5. Strictly protects secrets and credentials.
"""

from enum import Enum
import logging
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Tuple
import uuid

from pydantic import BaseModel, ConfigDict, Field

import redaction

logger = logging.getLogger(__name__)


class PersonalMemoryCategory(str, Enum):
    USER_FACT = "USER_FACT"
    USER_PREFERENCE = "USER_PREFERENCE"
    ROUTINE = "ROUTINE"
    PROJECT_CONTEXT = "PROJECT_CONTEXT"


class PersonalMemoryIntent(str, Enum):
    STORE = "STORE"
    FORGET = "FORGET"
    RETRIEVE = "RETRIEVE"
    NO_OP = "NO_OP"


class PersonalMemoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    memory_id: str
    category: PersonalMemoryCategory
    content: str
    created_at: float
    updated_at: float
    source: str = "user"
    status: str = "active"
    expires_at: Optional[float] = None
    enabled: bool = True


class MemoryCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    category: PersonalMemoryCategory
    content: str
    source: str = "user"
    expires_at: Optional[float] = None


class MemoryUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: Optional[str] = None
    enabled: Optional[bool] = None
    expires_at: Optional[float] = None


SECRET_PATTERNS = [
    r"-----BEGIN\s+(?:RSA\s+)?PRIVATE\s+KEY-----",
    r"bearer\s+[a-zA-Z0-9_\-\.]{10,}",
    r"ghp_[a-zA-Z0-9]{36}",
    r"sk-[a-zA-Z0-9]{20,}",
    r"AKIA[0-9A-Z]{16}",
    r"(?:password|passwd|api_key|apikey|secret|access_token|auth_token)\s*(?:[:=]|\bis\b)\s*\S+",
    r"\b(?:\d[ -]*?){13,16}\b",
]


def contains_sensitive_secret(text: str) -> bool:
    """Check if text contains credentials, private keys, bearer tokens, or secrets."""
    if not text:
        return False
    for pattern in SECRET_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def parse_memory_intent(
    user_text: str
) -> Tuple[PersonalMemoryIntent, Optional[str], Optional[PersonalMemoryCategory]]:
    """
    Parse natural language text for explicit memory control instructions:
    STORE, FORGET, RETRIEVE, or NO_OP.
    """
    if not user_text or not isinstance(user_text, str):
        return (PersonalMemoryIntent.NO_OP, None, None)

    cleaned = user_text.strip()

    # 1. FORGET intent detection
    forget_patterns = [
        r"forget\s+that\s+(.+)",
        r"forget\s+this:?\s+(.+)",
        r"forget\s+what\s+you\s+know\s+about\s+(.+)",
        r"forget\s+my\s+preference\s+(?:about\s+)?(.+)",
        r"forget\s+preference\s+(.+)",
        r"forget\s+(.+)",
    ]
    for pat in forget_patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            target = m.group(1).strip()
            return (PersonalMemoryIntent.FORGET, target, None)

    # 2. STORE intent detection
    store_patterns = [
        r"remember\s+that\s+(.+)",
        r"remember\s+this:?\s+(.+)",
        r"remember\s+my\s+project\s+is\s+(.+)",
        r"remember\s+(.+)",
    ]
    for pat in store_patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            content = m.group(1).strip()
            cat = PersonalMemoryCategory.USER_FACT
            content_lower = content.lower()
            if any(kw in content_lower for kw in ["prefer", "like", "favorite", "want", "answer", "format", "style", "explain"]):
                cat = PersonalMemoryCategory.USER_PREFERENCE
            elif any(kw in content_lower for kw in ["project", "working on", "building", "repo", "codebase", "evie"]):
                cat = PersonalMemoryCategory.PROJECT_CONTEXT
            elif any(kw in content_lower for kw in ["routine", "schedule", "daily", "always at"]):
                cat = PersonalMemoryCategory.ROUTINE
            return (PersonalMemoryIntent.STORE, content, cat)

    # 3. RETRIEVE intent detection
    retrieve_patterns = [
        r"what\s+do\s+you\s+remember\s*(?:about\s+(.+))?",
        r"what\s+are\s+my\s+preferences",
        r"show\s+my\s+memories",
        r"list\s+my\s+memories",
        r"do\s+you\s+remember\s+(.+)",
        r"how\s+should\s+you\s+explain\s+things",
    ]
    for pat in retrieve_patterns:
        m = re.search(pat, cleaned, re.IGNORECASE)
        if m:
            target = m.group(1).strip() if m.lastindex and m.group(1) else cleaned
            return (PersonalMemoryIntent.RETRIEVE, target, None)

    return (PersonalMemoryIntent.NO_OP, None, None)


class PersonalMemoryManager:
    """
    Local, restart-safe Personal Memory Manager backed by SQLite.
    """

    def __init__(self, db_path: str = "evie.db"):
        self.db_path = db_path
        self._audit_logs: List[Dict[str, Any]] = []
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS personal_memories (
                    memory_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    source TEXT NOT NULL,
                    status TEXT NOT NULL,
                    expires_at REAL,
                    enabled INTEGER NOT NULL
                );
                """
            )
            conn.commit()

    def _log_audit(self, event: str, details: Dict[str, Any]) -> None:
        sanitized_details = redaction.redact_dict_secrets(details)
        record = {
            "event": event,
            "timestamp": time.time(),
            "details": sanitized_details,
        }
        self._audit_logs.append(record)
        logger.info(f"AUDIT {event}: {sanitized_details}")

    def get_audit_logs(self) -> List[Dict[str, Any]]:
        return list(self._audit_logs)

    def create_memory(
        self,
        category: PersonalMemoryCategory,
        content: str,
        source: str = "user",
        expires_at: Optional[float] = None,
    ) -> PersonalMemoryEntry:
        if not content or not content.strip():
            raise ValueError("Memory content cannot be empty.")

        if contains_sensitive_secret(content):
            self._log_audit("MEMORY_REJECTED", {"reason": "secret_detected", "category": str(category)})
            raise ValueError("Memory contains sensitive secret or credential and cannot be stored.")

        content_clean = content.strip()[:500]
        now = time.time()

        # Deduplication / Preference update check
        existing = self._find_matching_memory(category, content_clean)
        if existing:
            updated = self.update_memory(
                existing.memory_id,
                content=content_clean,
                enabled=True,
                expires_at=expires_at,
            )
            if updated:
                return updated

        memory_id = f"mem_{uuid.uuid4().hex[:12]}"
        entry = PersonalMemoryEntry(
            memory_id=memory_id,
            category=category,
            content=content_clean,
            created_at=now,
            updated_at=now,
            source=source,
            status="active",
            expires_at=expires_at,
            enabled=True,
        )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO personal_memories
                (memory_id, category, content, created_at, updated_at, source, status, expires_at, enabled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.memory_id,
                    entry.category.value if isinstance(entry.category, Enum) else str(entry.category),
                    entry.content,
                    entry.created_at,
                    entry.updated_at,
                    entry.source,
                    entry.status,
                    entry.expires_at,
                    1 if entry.enabled else 0,
                ),
            )
            conn.commit()

        self._log_audit(
            "MEMORY_CREATED",
            {
                "memory_id": entry.memory_id,
                "category": str(entry.category),
                "content": entry.content,
            },
        )
        return entry

    def _find_matching_memory(
        self, category: PersonalMemoryCategory, content: str
    ) -> Optional[PersonalMemoryEntry]:
        memories = self.list_memories(category=category, enabled_only=True)
        if not memories:
            return None

        content_words = set(re.findall(r"\w+", content.lower()))
        for m in memories:
            m_words = set(re.findall(r"\w+", m.content.lower()))
            if category == PersonalMemoryCategory.USER_PREFERENCE:
                pref_keywords = {"explain", "answer", "answers", "concise", "detailed", "style", "language", "format"}
                if (content_words & pref_keywords) and (m_words & pref_keywords):
                    return m
            elif category == PersonalMemoryCategory.PROJECT_CONTEXT:
                proj_keywords = {"project", "repo", "codebase", "building", "working"}
                if (content_words & proj_keywords) and (m_words & proj_keywords):
                    return m

            common = content_words & m_words
            if len(common) >= 3 and len(common) / max(len(content_words), 1) > 0.4:
                return m

        return None

    def get_memory(self, memory_id: str) -> Optional[PersonalMemoryEntry]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM personal_memories WHERE memory_id = ?", (memory_id,)
            ).fetchone()
        if not row:
            return None
        return self._row_to_entry(row)

    def list_memories(
        self,
        category: Optional[PersonalMemoryCategory] = None,
        enabled_only: bool = True,
    ) -> List[PersonalMemoryEntry]:
        sql = "SELECT * FROM personal_memories WHERE 1=1"
        params: List[Any] = []
        if category:
            cat_str = category.value if isinstance(category, Enum) else str(category)
            sql += " AND category = ?"
            params.append(cat_str)
        if enabled_only:
            sql += " AND enabled = 1"
        sql += " ORDER BY updated_at DESC"

        with self._get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        now = time.time()
        results: List[PersonalMemoryEntry] = []
        for r in rows:
            entry = self._row_to_entry(r)
            if entry.expires_at is not None and entry.expires_at < now:
                continue
            results.append(entry)
        return results

    def update_memory(
        self,
        memory_id: str,
        content: Optional[str] = None,
        enabled: Optional[bool] = None,
        expires_at: Optional[float] = None,
    ) -> Optional[PersonalMemoryEntry]:
        entry = self.get_memory(memory_id)
        if not entry:
            return None

        now = time.time()
        new_content = entry.content
        if content is not None:
            if contains_sensitive_secret(content):
                self._log_audit("MEMORY_REJECTED", {"reason": "secret_detected", "memory_id": memory_id})
                raise ValueError("Updated content contains sensitive secret or credential.")
            new_content = content.strip()[:500]

        new_enabled = entry.enabled if enabled is None else enabled
        new_expires = entry.expires_at if expires_at is None else expires_at

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE personal_memories
                SET content = ?, enabled = ?, expires_at = ?, updated_at = ?
                WHERE memory_id = ?
                """,
                (new_content, 1 if new_enabled else 0, new_expires, now, memory_id),
            )
            conn.commit()

        updated_entry = self.get_memory(memory_id)
        self._log_audit(
            "MEMORY_UPDATED",
            {
                "memory_id": memory_id,
                "content": new_content,
                "enabled": new_enabled,
            },
        )
        return updated_entry

    def forget_memory(
        self,
        memory_id: Optional[str] = None,
        query: Optional[str] = None,
        category: Optional[PersonalMemoryCategory] = None,
    ) -> int:
        memories = self.list_memories(category=category, enabled_only=False)
        targets_to_delete: List[str] = []

        if memory_id:
            targets_to_delete.append(memory_id)
        elif query:
            q_lower = query.lower()
            q_words = set(re.findall(r"\w+", q_lower))
            for m in memories:
                m_lower = m.content.lower()
                if q_lower in m_lower or (q_words and set(re.findall(r"\w+", m_lower)) & q_words):
                    targets_to_delete.append(m.memory_id)
        elif category:
            for m in memories:
                targets_to_delete.append(m.memory_id)

        if not targets_to_delete:
            return 0

        with self._get_connection() as conn:
            placeholders = ",".join(["?"] * len(targets_to_delete))
            conn.execute(
                f"DELETE FROM personal_memories WHERE memory_id IN ({placeholders})",
                targets_to_delete,
            )
            conn.commit()

        for mid in targets_to_delete:
            self._log_audit("MEMORY_FORGOTTEN", {"memory_id": mid})

        return len(targets_to_delete)

    def clear_memories(self) -> None:
        with self._get_connection() as conn:
            conn.execute("DELETE FROM personal_memories")
            conn.commit()

    def get_relevant_memories(
        self, query: str, max_results: int = 5
    ) -> List[PersonalMemoryEntry]:
        all_memories = self.list_memories(enabled_only=True)
        if not all_memories:
            return []

        if not query or not query.strip():
            return all_memories[:max_results]

        q_lower = query.lower()
        q_words = set(re.findall(r"\w+", q_lower))

        scored: List[Tuple[float, PersonalMemoryEntry]] = []
        for m in all_memories:
            m_words = set(re.findall(r"\w+", m.content.lower()))
            overlap = len(q_words & m_words)
            category_match = 1.0 if m.category.value.lower() in q_lower else 0.0
            score = overlap * 2.0 + category_match
            if overlap > 0 or category_match > 0 or "explain" in q_lower or "how" in q_lower or "remember" in q_lower or "preference" in q_lower:
                scored.append((score, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        if not scored and any(kw in q_lower for kw in ["how", "what", "remember", "preference", "style"]):
            return all_memories[:max_results]

        return [item[1] for item in scored[:max_results]]

    def _row_to_entry(self, row: sqlite3.Row) -> PersonalMemoryEntry:
        return PersonalMemoryEntry(
            memory_id=row["memory_id"],
            category=PersonalMemoryCategory(row["category"]),
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            source=row["source"],
            status=row["status"],
            expires_at=row["expires_at"],
            enabled=bool(row["enabled"]),
        )


def format_memory_context(memories: List[PersonalMemoryEntry]) -> str:
    """
    Format memories safely as XML contextual data for LLM prompt context.
    Strips raw tags from memory content for memory injection defense.
    """
    if not memories:
        return ""

    lines = ["<user_memory>"]
    for m in memories:
        clean_content = re.sub(r"<[^>]+>", "", m.content).strip()
        lines.append(f"- [{m.category.value}] {clean_content}")
    lines.append("</user_memory>")

    return "\n".join(lines)


_GLOBAL_PERSONAL_MEMORY_MANAGER: Optional[PersonalMemoryManager] = None


def get_personal_memory_manager(db_path: str = "evie.db") -> PersonalMemoryManager:
    """Singleton getter for PersonalMemoryManager."""
    global _GLOBAL_PERSONAL_MEMORY_MANAGER
    if _GLOBAL_PERSONAL_MEMORY_MANAGER is None:
        _GLOBAL_PERSONAL_MEMORY_MANAGER = PersonalMemoryManager(db_path=db_path)
    return _GLOBAL_PERSONAL_MEMORY_MANAGER


def set_personal_memory_manager(manager: Optional[PersonalMemoryManager]) -> None:
    """Singleton setter for PersonalMemoryManager."""
    global _GLOBAL_PERSONAL_MEMORY_MANAGER
    _GLOBAL_PERSONAL_MEMORY_MANAGER = manager
