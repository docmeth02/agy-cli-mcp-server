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
_handle_locks: dict = {}


def _get_handle_lock(conversation_id: str) -> asyncio.Lock:
    """
    Get a per-conversation lock, bound to the current event loop.

    Held across the whole resolve -> run -> bind sequence. Deliberately NOT the
    global metadata lock: that is also used for short sidecar writes, and holding
    it for the duration of an agy run (up to the per-task timeout) would serialise
    every conversation in the server. Two concurrent turns on the *same* handle
    genuinely must serialise, though — otherwise both take the binding path, each
    creates an agy conversation, and the second binding orphans the first.
    """
    loop = asyncio.get_running_loop()
    key = (id(loop), conversation_id)
    lock = _handle_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _handle_locks[key] = lock
    return lock


def _release_handle_lock(conversation_id: str) -> None:
    """
    Drop a per-handle lock once nobody holds or awaits it.

    Without this the table grows one entry per distinct id forever, and a caller
    passing many ids (including ones rejected early) could grow it without bound.
    Keeping the entry while it is held or contended is essential — removing it
    then would let a second turn create a fresh lock and defeat the serialisation.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    key = (id(loop), conversation_id)
    lock = _handle_locks.get(key)
    if lock is not None and not lock.locked():
        _handle_locks.pop(key, None)


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
                loaded = json.load(f)
            # The sidecar is user-editable, so the root may be any JSON value.
            # Callers all treat it as a mapping; returning a list or string would
            # surface as an AttributeError escaping the MCP tool.
            if isinstance(loaded, dict):
                return loaded
            logger.error(
                f"Conversation metadata root is {type(loaded).__name__}, "
                f"expected object; ignoring it."
            )
            return {}
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
        """
        Create a conversation handle.

        The id minted here is an MCP-level handle, deliberately stable for the
        caller's whole session. agy does not know it yet: nothing is created
        server-side and no quota is spent. The handle is *bound* to a real agy
        conversation on the first continue_conversation() call, which runs without
        --conversation and records the id agy reports back in its JSON envelope
        (see agy_conversation_id below).
        """
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
                # None until the first turn binds this handle to an agy-side
                # conversation. An unbound handle is not resumable, because agy
                # silently ignores an id it has never seen.
                "agy_conversation_id": None,
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
            "bound": False,
            "note": (
                "Not yet backed by an agy conversation. The first "
                "gemini_continue_conversation call on this id starts the "
                "conversation and binds it; subsequent calls resume it."
            ),
        }

    async def continue_conversation(
        self,
        conversation_id: str,
        prompt: str,
        model: Optional[str] = None,
        project: Optional[str] = None,
    ) -> dict:
        """
        Continue a conversation via agy, binding the handle on its first turn.

        agy silently ignores an unknown ``--conversation`` id: it starts a
        brand-new conversation under a different id, exits 0, and emits no
        warning (verified on 1.1.11 in both text and JSON output modes). So the
        id must be one agy actually knows.

        Resolution order for the agy-side id:
          1. a recorded ``agy_conversation_id`` binding, else
          2. the handle itself, when a store already exists under that name
             (covers ids taken straight from gemini_list_conversations), else
          3. unbound — run WITHOUT ``--conversation`` and adopt the id agy
             reports in its JSON envelope, recording it as the binding.

        Case 3 needs the JSON transport to learn the id. On the text transport
        there is nothing to read it from, so an unbound handle is refused rather
        than silently starting a context that can never be resumed.
        """
        if not prompt or not prompt.strip():
            return {"status": "error", "error": "Prompt cannot be empty"}

        if not _is_valid_conversation_id(conversation_id):
            return _invalid_id_error(conversation_id)

        # Held for the whole resolve -> run -> bind sequence, so two concurrent
        # turns on this handle cannot both take the binding path (which would
        # create two agy conversations and orphan one, quota already spent).
        lock = _get_handle_lock(conversation_id)
        try:
            async with lock:
                return await self._continue_locked(
                    conversation_id, prompt, model, project
                )
        finally:
            _release_handle_lock(conversation_id)

    async def _continue_locked(
        self,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        project: Optional[str],
    ) -> dict:
        metadata = _load_metadata()
        raw_meta = metadata.get(conversation_id)
        meta = raw_meta if isinstance(raw_meta, dict) else {}
        bound_id = meta.get("agy_conversation_id")
        if bound_id and not _is_valid_conversation_id(bound_id):
            logger.warning(
                f"Ignoring malformed agy_conversation_id for {conversation_id}"
            )
            bound_id = None

        # Distinguish "never bound" from "binding broke": agy stores can be
        # cleaned up or expire, and silently rebinding would drop the history
        # while reporting a fresh successful binding.
        stale_binding = None
        if bound_id and _conversation_exists(bound_id):
            agy_id = bound_id
        elif _conversation_exists(conversation_id):
            agy_id = conversation_id
        else:
            agy_id = None  # unbound — this turn will create and adopt one
            if bound_id:
                stale_binding = bound_id

        from modules.utils.cli_utils import (
            _build_cli_args, resolve_output_format, _get_cached_or_sync_version,
            _parse_version,
        )

        if agy_id is None:
            cached_version = _get_cached_or_sync_version()
            try:
                transport = resolve_output_format(
                    _parse_version(cached_version) if cached_version else (0, 0, 0),
                    bool(cached_version),
                )
            except Exception as e:
                # CLI_OUTPUT_FORMAT=json on an agy that cannot produce it.
                return {
                    "status": "error",
                    "error": str(e),
                    "error_code": "CONFIG_ERROR",
                }
            if transport != "json":
                return {
                    "status": "error",
                    "error": (
                        f"Conversation {conversation_id} is not yet bound to an agy "
                        f"conversation, and binding requires agy's JSON transport "
                        f"(agy >= 1.1.8 with CLI_OUTPUT_FORMAT=auto or json). "
                        f"Without it the new conversation's id cannot be captured, "
                        f"so this turn would start a context that can never be "
                        f"resumed. Use gemini_prompt for a one-shot request, or "
                        f"pass an id from gemini_list_conversations whose "
                        f"has_native_file is true."
                    ),
                    "error_code": "CONVERSATION_NOT_BOUND",
                }

        args = _build_cli_args(
            prompt=prompt,
            # Omitted on the binding turn: passing an id agy does not know would
            # be silently discarded anyway.
            conversation_id=agy_id,
            model=model,
            project=project,
        )

        try:
            result = await execute_cli_with_retry(args)

            # Adopt whatever id agy actually used. On the binding turn this is
            # the new conversation; on later turns it should echo agy_id back,
            # and if it ever differs, agy started a different conversation and
            # the binding must follow it or every later turn loses history.
            reported = result.get("conversation_id")
            if reported and not _is_valid_conversation_id(reported):
                logger.warning(f"agy reported a malformed conversation id: {reported!r}")
                reported = None

            async with _get_metadata_lock():
                metadata = _load_metadata()
                now = time.time()
                entry = metadata.setdefault(conversation_id, {
                    "title": f"Conversation {conversation_id[:8]}",
                    "created_at": now,
                    "expiration_hours": DEFAULT_EXPIRATION_HOURS,
                })
                entry["updated_at"] = now
                if model:
                    entry["model"] = model
                if reported:
                    entry["agy_conversation_id"] = reported
                saved = _save_metadata(metadata)

            self._stats["messages_added"] += 1

            response = {
                "status": result.get("status", "success"),
                # The caller's stable handle, not agy's internal id.
                "conversation_id": conversation_id,
                "response": result.get("stdout", ""),
            }
            warnings: list[str] = []
            if stale_binding:
                warnings.append(
                    f"previous agy conversation {stale_binding} no longer exists, "
                    f"so earlier history was not carried forward"
                )

            if reported:
                response["agy_conversation_id"] = reported
                response["bound"] = True
                if agy_id is None:
                    response["bound_on_this_turn"] = True
                elif reported != agy_id:
                    warnings.append(
                        f"agy resumed a different conversation ({reported}) than "
                        f"requested ({agy_id}); the binding has been updated"
                    )
            elif agy_id is None:
                # The binding turn captured no usable id, so the handle is still
                # unbound: every later turn would take this path again and start
                # a fresh context while reporting success. Say so rather than
                # letting history silently fail to accumulate.
                response["bound"] = False
                warnings.append(
                    "this turn could not be bound to an agy conversation (no "
                    "usable conversation_id was reported), so its history will "
                    "not carry forward to the next turn"
                )
            else:
                response["bound"] = True
            for key in ("usage", "num_turns", "error"):
                if result.get(key) is not None:
                    response[key] = result[key]
            if model:
                response["model"] = model
            if not saved:
                warnings.append("metadata update failed")
            if warnings:
                response["warning"] = " | ".join(warnings)
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

        Ordered by recency rather than metadata-first. An unbound sidecar entry
        (a handle from gemini_start_conversation whose first turn has not run
        yet) would otherwise occupy the head of the list permanently and push
        resumable conversations past `limit`.

        `has_native_file` reports whether an id can be continued, resolving
        through `agy_conversation_id` when the handle is bound. `bound` and
        `agy_conversation_id` expose the binding itself.
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
            meta = metadata.get(cid)
            meta = meta if isinstance(meta, dict) else {}
            bound_id = meta.get("agy_conversation_id")
            candidates = [
                _get_conversation_mtime(cid),
                _num(meta.get("updated_at"), 0.0),
                _num(meta.get("created_at"), 0.0),
            ]
            if bound_id and _is_valid_conversation_id(bound_id):
                candidates.append(_get_conversation_mtime(bound_id))
            return max(candidates)

        all_ids.sort(key=_recency, reverse=True)

        for cid in all_ids:
            raw = metadata.get(cid)
            meta = raw if isinstance(raw, dict) else {}
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

            # Resolve through the binding: a handle from start_conversation has
            # no store of its own, but once bound it is fully resumable. Reporting
            # has_native_file from the handle alone would tell callers a working
            # conversation is unusable.
            bound_id = meta.get("agy_conversation_id")
            if bound_id and not _is_valid_conversation_id(bound_id):
                bound_id = None
            resumable = _conversation_exists(cid) or bool(
                bound_id and _conversation_exists(bound_id)
            )

            conversations.append({
                "conversation_id": cid,
                "title": meta.get("title", f"Conversation {cid[:8]}"),
                "description": meta.get("description"),
                "tags": meta.get("tags", []),
                "created_at": created_at,
                "updated_at": meta.get("updated_at", mtime or created_at),
                "expiration_hours": expiration_hours,
                "message_count": 0,
                # True when this id can be continued, whether directly or via a
                # binding. Kept under the original name for compatibility.
                "has_native_file": resumable,
                "bound": bool(bound_id and _conversation_exists(bound_id)),
                "agy_conversation_id": bound_id,
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

        # Delete the store the handle is BOUND to, not just the handle's own
        # name. Deleting only the latter would remove the sidecar entry — the
        # sole pointer to agy's conversation — leaving that conversation on disk,
        # unreachable and undeletable, while reporting success.
        metadata_snapshot = _load_metadata()
        entry = metadata_snapshot.get(conversation_id)
        targets = [conversation_id]
        if isinstance(entry, dict):
            bound = entry.get("agy_conversation_id")
            if (
                bound
                and bound != conversation_id
                and _is_valid_conversation_id(bound)
            ):
                targets.append(bound)

        # Remove every known store format, plus SQLite's -wal/-shm companions —
        # leaving those behind would strand write-ahead data for a deleted id.
        for target in targets:
          for suffix in _CONVERSATION_SUFFIXES:
            for candidate in (
                CONVERSATIONS_DIR / f"{target}{suffix}",
                CONVERSATIONS_DIR / f"{target}{suffix}-wal",
                CONVERSATIONS_DIR / f"{target}{suffix}-shm",
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
