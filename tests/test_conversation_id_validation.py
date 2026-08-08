"""
Unit tests for conversation-id validation.

Conversation ids are interpolated into a filesystem path
(`CONVERSATIONS_DIR / f"{cid}.pb"`). Two pathlib behaviours make an unvalidated
id dangerous: dividing by an **absolute** string discards the base directory
entirely, and `..` segments are resolved by the kernel on access. Without
validation that turned existence checks into a filesystem oracle and
`clear_conversation()` into arbitrary file deletion.

No `agy` installation required.
"""
import asyncio

import pytest

from modules.services.conversation_manager import (
    ConversationManager,
    _conversation_exists,
    _get_conversation_mtime,
    _is_valid_conversation_id,
)

# Ids that must never reach the filesystem.
UNSAFE_IDS = [
    "/etc/passwd",                      # absolute — pathlib discards the base
    "/tmp/anything",
    "../../../../etc/passwd",           # traversal
    "../../etc/hosts",
    "..",
    ".",
    "a/b",                              # separator
    "a\\b",                             # windows-style separator
    "sub/dir/id",
    "",                                 # empty
    "   ",
    "-leading-dash",                    # could be read as a CLI flag
    "x" * 65,                           # over length
    "id with spaces",
    "id;rm -rf /",                      # shell metacharacters
    "id\nsecond-line",
    "id\x00null",
    "café",                             # non-ascii
]

SAFE_IDS = [
    "ea160180-a361-4cd8-808f-3252614f45cd",
    "3a4105bf-0583-408c-beeb-7a230c683785",
    "conv_12345",
    "abc123",
    "A",
    "a" * 64,
]


class TestIdValidation:

    @pytest.mark.parametrize("cid", UNSAFE_IDS)
    def test_unsafe_ids_rejected(self, cid):
        assert _is_valid_conversation_id(cid) is False

    @pytest.mark.parametrize("cid", SAFE_IDS)
    def test_safe_ids_accepted(self, cid):
        assert _is_valid_conversation_id(cid) is True

    def test_non_string_rejected(self):
        assert _is_valid_conversation_id(None) is False
        assert _is_valid_conversation_id(123) is False


class TestExistenceOracleClosed:
    """An invalid id must never be probed against the real filesystem."""

    @pytest.mark.parametrize("cid", UNSAFE_IDS)
    def test_exists_returns_false_without_touching_disk(self, cid):
        assert _conversation_exists(cid) is False

    @pytest.mark.parametrize("cid", UNSAFE_IDS)
    def test_mtime_returns_zero(self, cid):
        assert _get_conversation_mtime(cid) == 0.0

    def test_absolute_path_to_existing_file_is_not_reported_as_conversation(self, tmp_path):
        # The classic bypass: CONVERSATIONS_DIR / "/abs/path.pb" == "/abs/path.pb"
        real = tmp_path / "oracle.pb"
        real.write_text("x")
        assert _conversation_exists(str(tmp_path / "oracle")) is False


class TestClearConversationCannotDeleteArbitraryFiles:

    def test_absolute_id_does_not_unlink(self, tmp_path):
        victim = tmp_path / "victim.pb"
        victim.write_text("important")

        manager = ConversationManager()
        result = asyncio.run(manager.clear_conversation(str(tmp_path / "victim")))

        assert result["status"] == "error"
        assert result["error_code"] == "INVALID_CONVERSATION_ID"
        assert victim.exists(), "clear_conversation deleted a file outside the store"

    def test_traversal_id_does_not_unlink(self, tmp_path):
        victim = tmp_path / "victim2.pb"
        victim.write_text("important")
        traversal = "../" * 12 + str(tmp_path / "victim2").lstrip("/")

        manager = ConversationManager()
        result = asyncio.run(manager.clear_conversation(traversal))

        assert result["status"] == "error"
        assert victim.exists()


class TestContinueConversationRejectsUnsafeIds:

    @pytest.mark.parametrize("cid", ["/etc/passwd", "../../etc/hosts", "a/b", ""])
    def test_rejected_before_reaching_agy(self, cid):
        manager = ConversationManager()
        result = asyncio.run(manager.continue_conversation(cid, "hello"))
        assert result["status"] == "error"
        assert result["error_code"] == "INVALID_CONVERSATION_ID"

    def test_valid_but_unbound_id_reports_not_bound(self):
        # A well-formed id with no agy .pb behind it must be refused distinctly:
        # agy would silently ignore it and start a fresh, historyless context.
        manager = ConversationManager()
        result = asyncio.run(
            manager.continue_conversation("ffffffff-0000-0000-0000-000000000000", "hello")
        )
        assert result["status"] == "error"
        assert result["error_code"] == "CONVERSATION_NOT_BOUND"


class TestConversationStorageFormats:
    """
    agy migrated its conversation store from protobuf to SQLite: older
    conversations are "<uuid>.pb", newer ones "<uuid>.db" with "-wal"/"-shm"
    companions. Globbing only *.pb made every recent conversation invisible —
    listing skipped them, existence checks said no, and clear() reported
    "not found" while leaving the files in place.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import modules.services.conversation_manager as cm
        monkeypatch.setattr(cm, "CONVERSATIONS_DIR", tmp_path)
        monkeypatch.setattr(cm, "METADATA_FILE", tmp_path / "mcp_metadata.json")
        monkeypatch.setattr(cm, "_ensure_dirs", lambda: None)
        return tmp_path

    def test_sqlite_conversation_is_found(self, store):
        (store / "aaaaaaaa-0000-0000-0000-000000000001.db").write_text("x")
        assert _conversation_exists("aaaaaaaa-0000-0000-0000-000000000001") is True

    def test_legacy_protobuf_conversation_is_still_found(self, store):
        (store / "bbbbbbbb-0000-0000-0000-000000000002.pb").write_text("x")
        assert _conversation_exists("bbbbbbbb-0000-0000-0000-000000000002") is True

    def test_listing_includes_both_formats_and_excludes_companions(self, store):
        import modules.services.conversation_manager as cm
        (store / "aaaa1111-0000-0000-0000-000000000001.db").write_text("x")
        (store / "aaaa1111-0000-0000-0000-000000000001.db-wal").write_text("x")
        (store / "aaaa1111-0000-0000-0000-000000000001.db-shm").write_text("x")
        (store / "bbbb2222-0000-0000-0000-000000000002.pb").write_text("x")

        listed = cm._list_agy_conversations()
        assert sorted(listed) == [
            "aaaa1111-0000-0000-0000-000000000001",
            "bbbb2222-0000-0000-0000-000000000002",
        ], "WAL/SHM companions must not be listed as conversations"

    def test_id_present_in_both_formats_is_listed_once(self, store):
        import modules.services.conversation_manager as cm
        cid = "cccc3333-0000-0000-0000-000000000003"
        (store / f"{cid}.db").write_text("x")
        (store / f"{cid}.pb").write_text("x")
        assert cm._list_agy_conversations().count(cid) == 1

    def test_clear_removes_sqlite_store_and_companions(self, store):
        cid = "dddd4444-0000-0000-0000-000000000004"
        db = store / f"{cid}.db"
        wal = store / f"{cid}.db-wal"
        shm = store / f"{cid}.db-shm"
        for f in (db, wal, shm):
            f.write_text("x")

        result = asyncio.run(ConversationManager().clear_conversation(cid))

        assert result["status"] == "success"
        assert not db.exists(), "SQLite store not deleted"
        assert not wal.exists(), "WAL companion stranded after delete"
        assert not shm.exists(), "SHM companion stranded after delete"

    def test_clear_removes_legacy_protobuf_store(self, store):
        cid = "eeee5555-0000-0000-0000-000000000005"
        pb = store / f"{cid}.pb"
        pb.write_text("x")

        result = asyncio.run(ConversationManager().clear_conversation(cid))
        assert result["status"] == "success"
        assert not pb.exists()

    def test_mtime_reads_sqlite_store(self, store):
        cid = "ffff6666-0000-0000-0000-000000000006"
        (store / f"{cid}.db").write_text("x")
        assert _get_conversation_mtime(cid) > 0
