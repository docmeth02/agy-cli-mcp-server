"""
Conversation management backed by Antigravity CLI native conversations.

This module provides a thin wrapper around agy's native conversation storage
(~/.gemini/antigravity-cli/conversations/<uuid>.pb) with metadata tracking
in a local JSON sidecar file.
"""
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Optional

from modules.utils.cli_utils import execute_cli_with_retry

logger = logging.getLogger(__name__)

# Agy conversation storage paths
CONVERSATIONS_DIR = Path.home() / ".gemini" / "antigravity-cli" / "conversations"
METADATA_FILE = Path.home() / ".gemini" / "antigravity-cli" / "mcp_metadata.json"

DEFAULT_EXPIRATION_HOURS = 24


def _ensure_dirs():
    """Ensure conversation and metadata directories exist."""
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _load_metadata() -> dict:
    """Load conversation metadata from sidecar JSON."""
    if METADATA_FILE.exists():
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to load conversation metadata: {e}")
    return {}


def _save_metadata(data: dict) -> bool:
    """Save conversation metadata to sidecar JSON."""
    _ensure_dirs()
    try:
        with open(METADATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        return True
    except OSError as e:
        logger.error(f"Failed to save conversation metadata: {e}")
        return False


def _list_agy_conversations() -> list[str]:
    """List conversation UUIDs from agy's .pb files."""
    _ensure_dirs()
    return sorted(
        [f.stem for f in CONVERSATIONS_DIR.glob("*.pb")],
        key=lambda cid: (CONVERSATIONS_DIR / f"{cid}.pb").stat().st_mtime,
        reverse=True
    )


def _conversation_exists(conversation_id: str) -> bool:
    """Check if a conversation .pb file exists."""
    return (CONVERSATIONS_DIR / f"{conversation_id}.pb").exists()


def _get_conversation_mtime(conversation_id: str) -> float:
    """Get modification time of a conversation file."""
    path = CONVERSATIONS_DIR / f"{conversation_id}.pb"
    if path.exists():
        return path.stat().st_mtime
    return 0.0


class ConversationManager:
    """Manages conversations using agy's native storage."""

    def __init__(self):
        self._stats = {
            "conversations_created": 0,
            "messages_added": 0,
            "conversations_cleared": 0,
        }

    def create_conversation(
        self,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        expiration_hours: int = DEFAULT_EXPIRATION_HOURS
    ) -> dict:
        """Create a new conversation."""
        conversation_id = str(uuid.uuid4())
        now = time.time()

        metadata = _load_metadata()
        metadata[conversation_id] = {
            "title": title or f"Conversation {conversation_id[:8]}",
            "description": description,
            "tags": tags or [],
            "created_at": now,
            "updated_at": now,
            "expiration_hours": expiration_hours,
        }
        _save_metadata(metadata)

        self._stats["conversations_created"] += 1

        return {
            "status": "success",
            "conversation_id": conversation_id,
            "title": metadata[conversation_id]["title"],
            "description": description,
            "tags": tags or [],
            "created_at": now,
            "expiration_hours": expiration_hours,
            "message_count": 0,
        }

    async def continue_conversation(
        self,
        conversation_id: str,
        prompt: str,
        model: Optional[str] = None
    ) -> dict:
        """Continue an existing conversation via agy."""
        if not prompt or not prompt.strip():
            return {"status": "error", "error": "Prompt cannot be empty"}

        metadata = _load_metadata()
        meta = metadata.get(conversation_id)

        # If conversation doesn't exist in metadata, check if agy has a .pb for it
        if not meta and not _conversation_exists(conversation_id):
            return {"status": "error", "error": f"Conversation {conversation_id} not found"}

        # Build agy args
        from modules.utils.cli_utils import _build_cli_args
        args = _build_cli_args(
            prompt=prompt,
            conversation_id=conversation_id
        )

        try:
            result = await execute_cli_with_retry(args)

            # Reload metadata after await to avoid overwriting concurrent changes
            metadata = _load_metadata()
            now = time.time()
            if conversation_id in metadata:
                metadata[conversation_id]["updated_at"] = now
                _save_metadata(metadata)

            self._stats["messages_added"] += 1

            response = {
                "status": result.get("status", "success"),
                "conversation_id": conversation_id,
                "response": result.get("stdout", ""),
                "model_ignored": model is not None,
            }
            if result.get("stderr"):
                response["stderr"] = result["stderr"]
            return response

        except Exception as e:
            logger.error(f"Error continuing conversation: {e}")
            return {"status": "error", "error": str(e)}

    def list_conversations(
        self,
        limit: int = 20,
        status_filter: Optional[str] = None
    ) -> list[dict]:
        """List conversations from agy storage and metadata."""
        metadata = _load_metadata()
        agy_ids = _list_agy_conversations()

        # Merge: include all IDs that exist in either metadata or agy storage
        all_ids = list(dict.fromkeys(list(metadata.keys()) + agy_ids))

        conversations = []
        now = time.time()

        for cid in all_ids:
            meta = metadata.get(cid, {})
            mtime = _get_conversation_mtime(cid)
            created_at = meta.get("created_at", mtime or now)
            expiration_hours = meta.get("expiration_hours", DEFAULT_EXPIRATION_HOURS)

            if status_filter == "active":
                if now > created_at + (expiration_hours * 3600):
                    continue

            conversations.append({
                "conversation_id": cid,
                "title": meta.get("title", f"Conversation {cid[:8]}"),
                "description": meta.get("description"),
                "tags": meta.get("tags", []),
                "created_at": created_at,
                "updated_at": meta.get("updated_at", mtime or created_at),
                "expiration_hours": expiration_hours,
                "message_count": 0,
                "has_native_file": _conversation_exists(cid),
            })

            if len(conversations) >= limit:
                break

        return conversations

    def clear_conversation(self, conversation_id: str) -> dict:
        """Clear/delete a conversation."""
        deleted = False

        # Delete agy .pb file
        pb_path = CONVERSATIONS_DIR / f"{conversation_id}.pb"
        if pb_path.exists():
            try:
                pb_path.unlink()
                deleted = True
            except OSError as e:
                logger.error(f"Failed to delete conversation file {conversation_id}: {e}")

        # Delete metadata entry
        metadata = _load_metadata()
        if conversation_id in metadata:
            del metadata[conversation_id]
            _save_metadata(metadata)
            deleted = True

        if deleted:
            self._stats["conversations_cleared"] += 1
            return {"status": "success", "message": f"Conversation {conversation_id} cleared"}

        return {"status": "error", "message": f"Conversation {conversation_id} not found"}

    def get_stats(self) -> dict:
        """Get conversation system statistics."""
        agy_ids = _list_agy_conversations()
        metadata = _load_metadata()

        total_size = 0
        for cid in agy_ids:
            path = CONVERSATIONS_DIR / f"{cid}.pb"
            if path.exists():
                total_size += path.stat().st_size

        return {
            **self._stats,
            "active_conversations": len(agy_ids),
            "tracked_metadata_entries": len(metadata),
            "total_storage_bytes": total_size,
            "storage_directory": str(CONVERSATIONS_DIR),
        }
