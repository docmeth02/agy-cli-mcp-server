"""
Conversation management backed by Antigravity CLI native conversations.

This module provides a thin wrapper around agy's native conversation storage
(~/.gemini/antigravity-cli/conversations/<uuid>.db for recent conversations,
legacy <uuid>.pb for older ones) with metadata tracking in a local JSON
sidecar file.
"""
import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
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

# Conversation ids are interpolated straight into a filesystem path
# (CONVERSATIONS_DIR / f"{cid}.pb"). Path division with an *absolute* string
# silently discards the base directory, and ".." segments are resolved by the
# kernel — so an unvalidated id escapes the conversations directory entirely.
# That would turn existence checks into a filesystem oracle and clear() into
# arbitrary file deletion. agy's own ids are UUIDs; accept nothing else.
_CONVERSATION_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def _is_valid_conversation_id(conversation_id: str) -> bool:
    """
    Whether a conversation id is safe to interpolate into a storage path.

    Rejects absolute paths, "..", separators and anything else that could
    resolve outside CONVERSATIONS_DIR. Kept deliberately strict rather than
    sanitising, so a malformed id is a loud error and never a silent redirect.
    """
    if not isinstance(conversation_id, str):
        return False
    if not _CONVERSATION_ID_RE.match(conversation_id):
        return False
    # Belt and braces: confirm the path lands directly inside the store.
    # Resolve the *parent*, not the leaf — resolving the leaf would follow a
    # symlink at the conversation file itself, so a stray or dangling
    # "<id>.pb" symlink pointing elsewhere would make us reject an otherwise
    # perfectly valid (and possibly .db-backed) conversation.
    parent = (CONVERSATIONS_DIR / f"{conversation_id}{_CONVERSATION_SUFFIXES[0]}").parent.resolve()
    return parent == CONVERSATIONS_DIR.resolve()


def _invalid_id_error(conversation_id: str) -> dict:
    """Standard error payload for a rejected conversation id."""
    return {
        "status": "error",
        "error": (
            "Invalid conversation_id: expected an agy conversation UUID "
            "(letters, digits, '-' and '_' only, max 64 chars). "
            f"Got {conversation_id!r}."
        ),
        "error_code": "INVALID_CONVERSATION_ID",
    }

_metadata_locks: dict = {}


def _get_metadata_lock() -> asyncio.Lock:
    """Get an asyncio.Lock bound to the current event loop."""
    loop = asyncio.get_running_loop()
    lock = _metadata_locks.get(id(loop))
    if lock is None:
        lock = asyncio.Lock()
        _metadata_locks[id(loop)] = lock
    return lock


def _ensure_dirs():
    """Ensure conversation and metadata directories exist."""
    CONVERSATIONS_DIR.mkdir(parents=True, exist_ok=True)


def _load_metadata() -> dict:
    """Load conversation metadata from sidecar JSON."""
    if METADATA_FILE.exists():
        try:
            with open(METADATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError as e:
            backup = METADATA_FILE.with_suffix(f".corrupt.{int(time.time())}.json")
            try:
                shutil.copy2(METADATA_FILE, backup)
                logger.error(f"Corrupt metadata backed up to {backup}: {e}")
            except OSError:
                logger.error(f"Corrupt metadata and backup failed: {e}")
        except OSError as e:
            logger.warning(f"Failed to read conversation metadata: {e}")
    return {}


def _save_metadata(data: dict) -> bool:
    """Save conversation metadata atomically via temp file + os.replace."""
    _ensure_dirs()
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=METADATA_FILE.parent,
            suffix=".tmp",
            delete=False,
            encoding="utf-8",
        ) as tmp_fd:
            tmp_path = tmp_fd.name
            json.dump(data, tmp_fd, indent=2)
            tmp_fd.flush()
            os.fsync(tmp_fd.fileno())
        os.replace(tmp_path, METADATA_FILE)
        return True
    except Exception as e:
        logger.error(f"Failed to save conversation metadata: {e}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        return False


# agy changed its conversation store from protobuf to SQLite: conversations
# created up to ~mid-2026 are "<uuid>.pb", newer ones are "<uuid>.db" (with
# "-wal"/"-shm" companions). Globbing only *.pb made every recent conversation
# invisible to this module — listing skipped them, existence checks said no, and
# clear() reported "not found" while leaving the files on disk. Both extensions
# must be recognised, newest format first.
_CONVERSATION_SUFFIXES = (".db", ".pb")


def _conversation_path(conversation_id: str) -> Optional[Path]:
    """
    The on-disk file backing a conversation, or None if agy has no store for it.

    Checks every known storage format. Assumes the id has already been
    validated by _is_valid_conversation_id().
    """
    for suffix in _CONVERSATION_SUFFIXES:
        candidate = CONVERSATIONS_DIR / f"{conversation_id}{suffix}"
        if candidate.exists():
            return candidate
    return None


def _list_agy_conversations() -> list[str]:
    """List conversation UUIDs from agy's on-disk stores (.db and legacy .pb)."""
    _ensure_dirs()
    # A dict keyed by stem de-duplicates an id that has both formats present.
    # "*.db" does not match "<uuid>.db-wal"/"-shm", so companions are excluded.
    found: dict[str, float] = {}
    for suffix in _CONVERSATION_SUFFIXES:
        for f in CONVERSATIONS_DIR.glob(f"*{suffix}"):
            try:
                mtime = f.stat().st_mtime
            except OSError:
                continue
            if f.stem not in found or mtime > found[f.stem]:
                found[f.stem] = mtime
    return sorted(found, key=lambda cid: found[cid], reverse=True)


def _conversation_exists(conversation_id: str) -> bool:
    """
    Whether agy has an on-disk store for this conversation (.db or legacy .pb).

    Returns False for any id that fails validation, so a traversal or absolute
    path can never be probed through this function.
    """
    if not _is_valid_conversation_id(conversation_id):
        return False
    return _conversation_path(conversation_id) is not None


def _get_conversation_mtime(conversation_id: str) -> float:
    """Get modification time of a conversation's store file."""
    if not _is_valid_conversation_id(conversation_id):
        return 0.0
    path = _conversation_path(conversation_id)
    if path is None:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


class ConversationManager:
    """Manages conversations using agy's native storage."""

    def __init__(self):
        self._stats = {
            "conversations_created": 0,
            "messages_added": 0,
            "conversations_cleared": 0,
        }

    async def create_conversation(
        self,
        title: Optional[str] = None,
        description: Optional[str] = None,
        tags: Optional[list[str]] = None,
        expiration_hours: int = DEFAULT_EXPIRATION_HOURS
    ) -> dict:
        """Create a new conversation."""
        conversation_id = str(uuid.uuid4())
        now = time.time()

        async with _get_metadata_lock():
            metadata = _load_metadata()
            metadata[conversation_id] = {
                "title": title or f"Conversation {conversation_id[:8]}",
                "description": description,
                "tags": tags or [],
                "created_at": now,
                "updated_at": now,
                "expiration_hours": expiration_hours,
            }
            saved = _save_metadata(metadata)

        if not saved:
            return {"status": "error", "error": "Failed to persist conversation metadata"}

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
        model: Optional[str] = None,
        project: Optional[str] = None,
    ) -> dict:
        """Continue an existing conversation via agy."""
        if not prompt or not prompt.strip():
            return {"status": "error", "error": "Prompt cannot be empty"}

        if not _is_valid_conversation_id(conversation_id):
            return _invalid_id_error(conversation_id)

        # agy silently ignores an unknown --conversation id: it starts a brand-new
        # conversation under a different id, exits 0, and emits no warning
        # (verified on 1.1.11, in both text and JSON output modes). Since
        # create_conversation() mints its own uuid4 that agy never sees, passing
        # it through would produce a fresh, historyless context on every call
        # while still reporting success. Require a real agy-side store instead of
        # silently losing history — this subsumes the sidecar-metadata check,
        # because a sidecar entry alone is not something agy can resume. The
        # real fix, binding the sidecar entry to the id agy reports back in its
        # JSON envelope, needs the JSON transport.
        if not _conversation_exists(conversation_id):
            return {
                "status": "error",
                "error": (
                    f"Conversation {conversation_id} has no agy-side history yet, "
                    f"so continuing it would silently start a new context instead "
                    f"of resuming. Use gemini_prompt for a one-shot request, or "
                    f"pass a conversation_id from gemini_list_conversations whose "
                    f"has_native_file is true."
                ),
                "error_code": "CONVERSATION_NOT_BOUND",
            }

        from modules.utils.cli_utils import _build_cli_args
        args = _build_cli_args(
            prompt=prompt,
            conversation_id=conversation_id,
            model=model,
            project=project,
        )

        try:
            result = await execute_cli_with_retry(args)

            async with _get_metadata_lock():
                metadata = _load_metadata()
                now = time.time()
                if conversation_id in metadata:
                    metadata[conversation_id]["updated_at"] = now
                    if model:
                        metadata[conversation_id]["model"] = model
                    saved = _save_metadata(metadata)
                else:
                    saved = True

            self._stats["messages_added"] += 1

            response = {
                "status": result.get("status", "success"),
                "conversation_id": conversation_id,
                "response": result.get("stdout", ""),
            }
            if model:
                response["model"] = model
            if not saved:
                response["warning"] = "metadata update failed"
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
        """
        List conversations from agy storage and metadata, most recent first.

        Ordered by recency rather than metadata-first. Sidecar-only entries (from
        gemini_start_conversation, which agy never learns about) would otherwise
        occupy the head of the list permanently and push real, resumable
        conversations past `limit` — starving the very workflow the tool
        docstrings prescribe, which is to pick an id whose has_native_file is
        true from this listing.
        """
        metadata = _load_metadata()
        agy_ids = _list_agy_conversations()

        # Merge: include all IDs that exist in either metadata or agy storage
        all_ids = list(dict.fromkeys(agy_ids + list(metadata.keys())))

        conversations = []
        now = time.time()

        def _num(value, fallback: float) -> float:
            """Coerce a sidecar timestamp to a float, tolerating junk.

            The sidecar is user-editable JSON, so any field may be null, a
            string, or missing. Arithmetic on those raises TypeError, which
            would escape gemini_list_conversations — the very listing the
            conversation docstrings tell callers to use to recover.
            """
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return fallback
            return float(value)

        def _recency(cid: str) -> float:
            meta = metadata.get(cid, {})
            return max(
                _get_conversation_mtime(cid),
                _num(meta.get("updated_at"), 0.0),
                _num(meta.get("created_at"), 0.0),
            )

        all_ids.sort(key=_recency, reverse=True)

        for cid in all_ids:
            meta = metadata.get(cid, {})
            mtime = _get_conversation_mtime(cid)
            created_at = _num(meta.get("created_at"), mtime or now)
            expiration_hours = _num(
                meta.get("expiration_hours"), DEFAULT_EXPIRATION_HOURS
            )

            expired = now > created_at + (expiration_hours * 3600)
            # "expired" was previously accepted and silently ignored, so the
            # filter returned every conversation.
            if status_filter == "active" and expired:
                continue
            if status_filter == "expired" and not expired:
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

    async def clear_conversation(self, conversation_id: str) -> dict:
        """Clear/delete a conversation."""
        # This method unlinks a path built from the id, so an unvalidated id
        # would be arbitrary file deletion (an absolute id discards the base
        # directory entirely under pathlib's `/`). Validate before touching disk.
        if not _is_valid_conversation_id(conversation_id):
            return _invalid_id_error(conversation_id)

        deleted = False

        # Remove every known store format, plus SQLite's -wal/-shm companions —
        # leaving those behind would strand write-ahead data for a deleted id.
        for suffix in _CONVERSATION_SUFFIXES:
            for candidate in (
                CONVERSATIONS_DIR / f"{conversation_id}{suffix}",
                CONVERSATIONS_DIR / f"{conversation_id}{suffix}-wal",
                CONVERSATIONS_DIR / f"{conversation_id}{suffix}-shm",
            ):
                if not candidate.exists():
                    continue
                try:
                    candidate.unlink()
                    # Only the primary store counts as "the conversation".
                    if candidate.suffix == suffix:
                        deleted = True
                except OSError as e:
                    logger.error(
                        f"Failed to delete conversation file {candidate.name}: {e}"
                    )

        async with _get_metadata_lock():
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
            if not _is_valid_conversation_id(cid):
                continue
            path = _conversation_path(cid)
            if path is not None:
                try:
                    total_size += path.stat().st_size
                except OSError:
                    pass

        return {
            **self._stats,
            "active_conversations": len(agy_ids),
            "tracked_metadata_entries": len(metadata),
            "total_storage_bytes": total_size,
            "storage_directory": str(CONVERSATIONS_DIR),
        }
