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


@pytest.fixture
def isolated_store(tmp_path, monkeypatch):
    """Point the manager at a throwaway store so no test touches real ~/.gemini."""
    import modules.services.conversation_manager as cm
    store = tmp_path / "conversations"
    store.mkdir()
    monkeypatch.setattr(cm, "CONVERSATIONS_DIR", store)
    monkeypatch.setattr(cm, "METADATA_FILE", tmp_path / "mcp_metadata.json")
    monkeypatch.setattr(cm, "_ensure_dirs", lambda: None)
    return store


class TestClearConversationCannotDeleteArbitraryFiles:

    def test_absolute_id_does_not_unlink(self, tmp_path, isolated_store):
        victim = tmp_path / "victim.pb"
        victim.write_text("important")

        manager = ConversationManager()
        result = asyncio.run(manager.clear_conversation(str(tmp_path / "victim")))

        assert result["status"] == "error"
        assert result["error_code"] == "INVALID_CONVERSATION_ID"
        assert victim.exists(), "clear_conversation deleted a file outside the store"

    def test_traversal_id_does_not_unlink(self, tmp_path, isolated_store):
        victim = tmp_path / "victim2.pb"
        victim.write_text("important")
        traversal = "../" * 12 + str(tmp_path / "victim2").lstrip("/")

        manager = ConversationManager()
        result = asyncio.run(manager.clear_conversation(traversal))

        assert result["status"] == "error"
        assert victim.exists()


class TestContinueConversationRejectsUnsafeIds:

    @pytest.mark.parametrize("cid", ["/etc/passwd", "../../etc/hosts", "a/b", ""])
    def test_rejected_before_reaching_agy(self, cid, isolated_store):
        manager = ConversationManager()
        result = asyncio.run(manager.continue_conversation(cid, "hello"))
        assert result["status"] == "error"
        assert result["error_code"] == "INVALID_CONVERSATION_ID"

    def test_unbound_id_is_refused_on_the_text_transport(self, isolated_store, monkeypatch):
        # Binding needs the JSON envelope to learn the id agy actually created.
        # Without it, continuing an unbound handle would start a context that
        # can never be resumed, so it must be refused rather than silently lost.
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "text")

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
        # Must fail if the de-duplicating dict is replaced by a naive
        # concatenation of the two globs, so assert on the whole listing rather
        # than only this id's count (a .pb-only glob would also yield 1).
        import modules.services.conversation_manager as cm
        both = "cccc3333-0000-0000-0000-000000000003"
        db_only = "dddd0000-0000-0000-0000-00000000000d"
        (store / f"{both}.db").write_text("x")
        (store / f"{both}.pb").write_text("x")
        (store / f"{db_only}.db").write_text("x")

        listed = cm._list_agy_conversations()
        assert sorted(listed) == sorted([both, db_only])
        assert len(listed) == 2

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


class TestListingOrder:
    """
    Sidecar-only entries (from gemini_start_conversation, which agy never learns
    about) must not occupy the head of the listing. They are unbindable, so
    pushing real resumable conversations past `limit` would starve the recovery
    workflow the tool docstrings prescribe: pick an id whose has_native_file is
    true from this listing.
    """

    def test_resumable_conversations_are_not_starved_by_metadata_entries(
        self, tmp_path, monkeypatch
    ):
        import modules.services.conversation_manager as cm

        store = tmp_path / "conversations"
        store.mkdir()
        monkeypatch.setattr(cm, "CONVERSATIONS_DIR", store)
        monkeypatch.setattr(cm, "METADATA_FILE", tmp_path / "mcp_metadata.json")
        monkeypatch.setattr(cm, "_ensure_dirs", lambda: None)

        # One real, resumable conversation.
        real_id = "aaaa9999-0000-0000-0000-00000000000a"
        (store / f"{real_id}.db").write_text("x")

        # 30 sidecar-only entries, i.e. 30 gemini_start_conversation calls.
        import json as _json
        meta = {
            f"bbbb{i:04d}-0000-0000-0000-00000000000b": {
                "title": f"unbindable {i}",
                "created_at": 1.0,
                "updated_at": 1.0,
                "expiration_hours": 24,
            }
            for i in range(30)
        }
        (tmp_path / "mcp_metadata.json").write_text(_json.dumps(meta))

        listed = cm.ConversationManager().list_conversations(limit=20)

        assert len(listed) == 20
        resumable = [c for c in listed if c["has_native_file"]]
        assert resumable, (
            "the only resumable conversation was pushed out of the listing by "
            "unbindable sidecar-only entries"
        )
        assert resumable[0]["conversation_id"] == real_id


class TestClearConversationOutcomes:

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import modules.services.conversation_manager as cm
        s = tmp_path / "conversations"
        s.mkdir()
        monkeypatch.setattr(cm, "CONVERSATIONS_DIR", s)
        monkeypatch.setattr(cm, "METADATA_FILE", tmp_path / "mcp_metadata.json")
        monkeypatch.setattr(cm, "_ensure_dirs", lambda: None)
        return s

    def test_valid_but_absent_id_reports_not_found(self, store):
        result = asyncio.run(
            ConversationManager().clear_conversation(
                "99999999-0000-0000-0000-000000000009"
            )
        )
        assert result["status"] == "error"
        assert "not found" in result["message"].lower()

    def test_metadata_only_entry_can_be_cleared(self, store, tmp_path):
        import json as _json
        cid = "77777777-0000-0000-0000-000000000007"
        (tmp_path / "mcp_metadata.json").write_text(
            _json.dumps({cid: {"title": "sidecar only"}})
        )
        result = asyncio.run(ConversationManager().clear_conversation(cid))
        assert result["status"] == "success"


class TestContinueConversationArgs:
    """`project` was accepted by the tool but never forwarded to agy, so session
    isolation silently did not apply."""

    @pytest.fixture
    def bound_store(self, tmp_path, monkeypatch):
        import modules.services.conversation_manager as cm
        store = tmp_path / "conversations"
        store.mkdir()
        monkeypatch.setattr(cm, "CONVERSATIONS_DIR", store)
        monkeypatch.setattr(cm, "METADATA_FILE", tmp_path / "mcp_metadata.json")
        monkeypatch.setattr(cm, "_ensure_dirs", lambda: None)
        cid = "12341234-0000-0000-0000-000000000001"
        (store / f"{cid}.db").write_text("x")
        return cid

    def _capture(self, monkeypatch):
        """Replace the subprocess layer and capture the args it was handed."""
        import modules.services.conversation_manager as cm
        seen = {}

        async def _fake(args, *a, **kw):
            seen["args"] = args
            return {"status": "success", "stdout": "ok", "stderr": "", "return_code": 0}

        monkeypatch.setattr(cm, "execute_cli_with_retry", _fake)
        return seen

    def test_project_is_forwarded(self, bound_store, monkeypatch):
        seen = self._capture(monkeypatch)
        result = asyncio.run(
            ConversationManager().continue_conversation(
                bound_store, "hi", project="proj-abc"
            )
        )
        assert result["status"] == "success"
        args = seen["args"]
        assert "--project" in args
        assert args[args.index("--project") + 1] == "proj-abc"

    def test_conversation_id_is_forwarded(self, bound_store, monkeypatch):
        seen = self._capture(monkeypatch)
        asyncio.run(ConversationManager().continue_conversation(bound_store, "hi"))
        args = seen["args"]
        assert args[args.index("--conversation") + 1] == bound_store

    def test_no_project_means_no_flag(self, bound_store, monkeypatch):
        seen = self._capture(monkeypatch)
        asyncio.run(ConversationManager().continue_conversation(bound_store, "hi"))
        assert "--project" not in seen["args"]

    def test_prompt_is_literal_by_default(self, bound_store, monkeypatch):
        # continue_conversation relays caller text, so it must not let a leading
        # "/" be reinterpreted as an agy slash command.
        seen = self._capture(monkeypatch)
        asyncio.run(ConversationManager().continue_conversation(bound_store, "/clear"))
        assert "--disable-slash-commands" in seen["args"]


class TestListConversationsToleratesJunkMetadata:
    """
    The sidecar is user-editable JSON, so any field may be null, a string, or the
    wrong type. Arithmetic on those raises TypeError, which escapes
    gemini_list_conversations — the listing the conversation docstrings tell
    callers to use to find a resumable id.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import modules.services.conversation_manager as cm
        s = tmp_path / "conversations"
        s.mkdir()
        monkeypatch.setattr(cm, "CONVERSATIONS_DIR", s)
        monkeypatch.setattr(cm, "METADATA_FILE", tmp_path / "mcp_metadata.json")
        monkeypatch.setattr(cm, "_ensure_dirs", lambda: None)
        return tmp_path

    @pytest.mark.parametrize("entry", [
        {"created_at": None},
        {"created_at": "2026-01-01T00:00:00Z"},
        {"expiration_hours": None},
        {"expiration_hours": "24"},
        {"created_at": True},
        {"updated_at": "not-a-number"},
        {"created_at": [], "expiration_hours": {}},
        {},
    ])
    @pytest.mark.parametrize("status_filter", [None, "active", "expired"])
    def test_does_not_raise(self, store, entry, status_filter):
        import json as _json
        (store / "mcp_metadata.json").write_text(
            _json.dumps({"aaaa1111-0000-0000-0000-00000000000a": entry})
        )
        # Must return a list, not raise.
        result = ConversationManager().list_conversations(status_filter=status_filter)
        assert isinstance(result, list)


class TestConversationBinding:
    """
    gemini_start_conversation mints an MCP-level handle that agy has never seen.
    Because agy silently ignores an unknown --conversation id (starting a new
    context under a different id while reporting success), the handle must be
    bound to the id agy reports in its JSON envelope on the first turn.
    """

    @pytest.fixture
    def store(self, tmp_path, monkeypatch):
        import modules.services.conversation_manager as cm
        import modules.utils.cli_utils as cu
        s = tmp_path / "conversations"
        s.mkdir()
        monkeypatch.setattr(cm, "CONVERSATIONS_DIR", s)
        monkeypatch.setattr(cm, "METADATA_FILE", tmp_path / "mcp_metadata.json")
        monkeypatch.setattr(cm, "_ensure_dirs", lambda: None)
        # Binding needs the JSON transport, so pin a version that provides it
        # rather than depending on a real agy being installed.
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.11")
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
        return s

    def _fake_agy(self, monkeypatch, store, agy_id):
        """Stub the subprocess layer, mimicking agy creating/echoing a conversation."""
        import modules.services.conversation_manager as cm
        seen = {}

        async def _fake(args, *a, **kw):
            seen["args"] = list(args)
            # agy materialises the conversation on disk as it runs.
            (store / f"{agy_id}.db").write_text("x")
            return {
                "status": "success",
                "stdout": "answer",
                "stderr": "",
                "return_code": 0,
                "conversation_id": agy_id,
                "usage": {"total_tokens": 10, "cache_read_tokens": 5},
                "num_turns": 1,
            }

        monkeypatch.setattr(cm, "execute_cli_with_retry", _fake)
        return seen

    def test_first_turn_binds_and_omits_conversation_flag(self, store, monkeypatch):
        agy_id = "bbbb0001-0000-0000-0000-00000000000b"
        seen = self._fake_agy(monkeypatch, store, agy_id)

        mgr = ConversationManager()
        created = asyncio.run(mgr.create_conversation(title="t"))
        handle = created["conversation_id"]
        assert created["bound"] is False

        result = asyncio.run(mgr.continue_conversation(handle, "hello"))

        assert result["status"] == "success"
        assert result["bound_on_this_turn"] is True
        assert result["agy_conversation_id"] == agy_id
        # The caller's handle stays stable regardless of agy's internal id.
        assert result["conversation_id"] == handle
        # The binding turn must NOT pass an id agy has never seen.
        assert "--conversation" not in seen["args"]

    def test_second_turn_resumes_using_the_bound_id(self, store, monkeypatch):
        agy_id = "bbbb0002-0000-0000-0000-00000000000b"
        seen = self._fake_agy(monkeypatch, store, agy_id)

        mgr = ConversationManager()
        handle = asyncio.run(mgr.create_conversation())["conversation_id"]
        asyncio.run(mgr.continue_conversation(handle, "first"))
        result = asyncio.run(mgr.continue_conversation(handle, "second"))

        assert result["status"] == "success"
        assert result.get("bound_on_this_turn") is None
        args = seen["args"]
        assert "--conversation" in args
        # Resumes agy's id, not the MCP handle.
        assert args[args.index("--conversation") + 1] == agy_id

    def test_binding_persists_in_the_sidecar(self, store, monkeypatch, tmp_path):
        import json as _json
        agy_id = "bbbb0003-0000-0000-0000-00000000000b"
        self._fake_agy(monkeypatch, store, agy_id)

        mgr = ConversationManager()
        handle = asyncio.run(mgr.create_conversation())["conversation_id"]
        asyncio.run(mgr.continue_conversation(handle, "hello"))

        meta = _json.loads((tmp_path / "mcp_metadata.json").read_text())
        assert meta[handle]["agy_conversation_id"] == agy_id

    def test_binding_follows_agy_if_it_reports_a_different_id(self, store, monkeypatch):
        # If agy ever resumes something other than what we asked for, following
        # it is the only way later turns keep the same history.
        first = "bbbb0004-0000-0000-0000-00000000000b"
        self._fake_agy(monkeypatch, store, first)
        mgr = ConversationManager()
        handle = asyncio.run(mgr.create_conversation())["conversation_id"]
        asyncio.run(mgr.continue_conversation(handle, "first"))

        second = "bbbb0005-0000-0000-0000-00000000000b"
        self._fake_agy(monkeypatch, store, second)
        result = asyncio.run(mgr.continue_conversation(handle, "second"))

        assert result["agy_conversation_id"] == second
        assert "resumed a different conversation" in result["warning"]

    def test_malformed_reported_id_is_ignored(self, store, monkeypatch):
        import modules.services.conversation_manager as cm

        async def _fake(args, *a, **kw):
            return {
                "status": "success", "stdout": "x", "stderr": "", "return_code": 0,
                "conversation_id": "../../etc/passwd",
            }

        monkeypatch.setattr(cm, "execute_cli_with_retry", _fake)
        mgr = ConversationManager()
        handle = asyncio.run(mgr.create_conversation())["conversation_id"]
        result = asyncio.run(mgr.continue_conversation(handle, "hello"))
        # Never adopt an id that could escape the conversations directory.
        assert "agy_conversation_id" not in result

    def test_id_from_listing_is_used_directly(self, store, monkeypatch):
        # An id taken straight from gemini_list_conversations already has a
        # store, so it should be passed through without a binding turn.
        existing = "cccc0001-0000-0000-0000-00000000000c"
        (store / f"{existing}.db").write_text("x")
        seen = self._fake_agy(monkeypatch, store, existing)

        result = asyncio.run(ConversationManager().continue_conversation(existing, "hi"))

        assert result["status"] == "success"
        args = seen["args"]
        assert args[args.index("--conversation") + 1] == existing
