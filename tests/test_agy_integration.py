"""
Integration tests against a real Antigravity CLI (agy) installation.

These tests validate every agy calling pattern used by the MCP server:
  --dangerously-skip-permissions  (always applied)
  --print <prompt>                (core prompt execution)
  --add-dir <path>                (file context via @filename expansion)
  --sandbox                       (sandbox/code execution mode)
  --conversation <id>             (resume a specific conversation)
  --continue                      (resume the most recent conversation)
  help                            (subcommand)
  --version                       (version query)

Each test exercises the real subprocess path through execute_cli / execute_cli_with_retry.
"""
import json
from pathlib import Path

import pytest
import pytest_asyncio

from modules.utils.cli_utils import (
    execute_cli,
    execute_cli_with_retry,
    _build_cli_args,
    extract_file_refs,
    sanitize_output,
    validate_cli_setup,
    CLITimeoutError,
)


# ---------------------------------------------------------------------------
# 1. CLI setup validation
# ---------------------------------------------------------------------------

class TestCLISetup:

    def test_agy_is_on_path(self):
        assert validate_cli_setup() is True

    @pytest.mark.asyncio
    async def test_version_returns_output(self):
        result = await execute_cli(["--version"], timeout=30)
        assert result["status"] == "success"
        assert result["return_code"] == 0
        assert result["stdout"].strip() != ""

    @pytest.mark.asyncio
    async def test_help_returns_output(self):
        result = await execute_cli(["help"], timeout=30)
        assert result["status"] == "success"
        combined = result["stdout"] + result["stderr"]
        assert "Usage" in combined or "agy" in combined


# ---------------------------------------------------------------------------
# 2. Basic prompt execution (--print)
# ---------------------------------------------------------------------------

class TestBasicPrompt:

    @pytest.mark.asyncio
    async def test_simple_prompt(self):
        """Core path: --dangerously-skip-permissions --print <prompt>"""
        args = _build_cli_args(prompt="Reply with exactly: PING_OK")
        result = await execute_cli_with_retry(args, timeout=60)
        assert result["status"] == "success"
        assert result["return_code"] == 0
        assert len(result["stdout"]) > 0

    @pytest.mark.asyncio
    async def test_prompt_has_skip_permissions(self):
        """Verify --dangerously-skip-permissions is always first arg."""
        args = _build_cli_args(prompt="test")
        assert args[0] == "--dangerously-skip-permissions"

    @pytest.mark.asyncio
    async def test_prompt_with_print_flag(self):
        """Verify --print is in the args."""
        args = _build_cli_args(prompt="hello world")
        idx = args.index("--print")
        assert args[idx + 1] == "hello world"


# ---------------------------------------------------------------------------
# 3. File context (--add-dir)
# ---------------------------------------------------------------------------

class TestFileContext:

    @pytest.mark.asyncio
    async def test_add_dir_single_file(self, sample_file):
        """Test --add-dir with a single file."""
        args = _build_cli_args(
            prompt="What does this code do? Reply in one sentence.",
            files=[str(sample_file)],
        )
        assert "--add-dir" in args
        assert str(sample_file) in args

        result = await execute_cli_with_retry(args, timeout=60)
        assert result["status"] == "success"
        assert result["return_code"] == 0

    @pytest.mark.asyncio
    async def test_add_dir_directory(self, sample_dir):
        """Test --add-dir with a directory path."""
        args = _build_cli_args(
            prompt="List the files you can see. Reply briefly.",
            files=[str(sample_dir)],
        )
        assert "--add-dir" in args

        result = await execute_cli_with_retry(args, timeout=60)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_add_dir_multiple_paths(self, sample_file, sample_dir):
        """Test --add-dir with multiple paths (file + directory)."""
        args = _build_cli_args(
            prompt="How many files do you see? Reply with a number.",
            files=[str(sample_file), str(sample_dir)],
        )
        add_dir_count = args.count("--add-dir")
        assert add_dir_count == 2

        result = await execute_cli_with_retry(args, timeout=60)
        assert result["status"] == "success"

    def test_extract_file_refs_resolves_real_file(self, monkeypatch):
        """Verify extract_file_refs finds an existing file within workspace."""
        workspace = Path(__file__).parent.parent.resolve()
        monkeypatch.chdir(workspace)
        prompt = "Analyze @mcp_server.py"
        cleaned, files = extract_file_refs(prompt)

        assert "@" not in cleaned
        assert len(files) == 1
        assert files[0] == str(workspace / "mcp_server.py")

    def test_extract_file_refs_skips_missing_file(self):
        """Verify extract_file_refs ignores non-existent paths."""
        cleaned, files = extract_file_refs("Analyze @/nonexistent/path.py")
        assert files == []

    def test_extract_file_refs_deduplicates(self, monkeypatch):
        """Same file referenced twice should appear once."""
        workspace = Path(__file__).parent.parent.resolve()
        monkeypatch.chdir(workspace)
        prompt = "Compare @mcp_server.py with @mcp_server.py"
        _, files = extract_file_refs(prompt)
        assert len(files) == 1

    def test_extract_file_refs_blocks_path_traversal(self, monkeypatch):
        """Paths outside workspace should be blocked."""
        workspace = Path(__file__).parent.parent.resolve()
        monkeypatch.chdir(workspace)
        _, files = extract_file_refs("Read @/etc/hosts")
        assert files == []

    def test_extract_file_refs_strips_trailing_punctuation(self, monkeypatch):
        """Trailing punctuation should be stripped from @refs."""
        workspace = Path(__file__).parent.parent.resolve()
        monkeypatch.chdir(workspace)
        _, files = extract_file_refs("Check @mcp_server.py, please")
        assert len(files) == 1


# ---------------------------------------------------------------------------
# 4. Sandbox mode (--sandbox)
# ---------------------------------------------------------------------------

class TestSandboxMode:

    @pytest.mark.asyncio
    async def test_sandbox_flag_in_args(self):
        args = _build_cli_args(prompt="print('hello')", sandbox=True)
        assert "--sandbox" in args

    @pytest.mark.asyncio
    async def test_sandbox_execution(self):
        """Test sandbox mode runs and returns a response."""
        args = _build_cli_args(
            prompt="Write a one-line Python script that prints 42, then run it.",
            sandbox=True,
        )
        result = await execute_cli_with_retry(args, timeout=120)
        assert result["status"] == "success"
        assert result["return_code"] == 0


# ---------------------------------------------------------------------------
# 5. Conversation management (--conversation, --continue)
# ---------------------------------------------------------------------------

class TestConversations:

    @pytest.mark.asyncio
    async def test_conversation_flag_construction(self):
        """Test --conversation flag is set correctly with a UUID."""
        import uuid
        conv_id = str(uuid.uuid4())
        args = _build_cli_args(
            prompt="Say OK",
            conversation_id=conv_id,
        )
        assert "--conversation" in args
        assert conv_id in args
        assert "--continue" not in args

    @pytest.mark.asyncio
    async def test_continue_flag(self):
        """Test --continue flag is set correctly."""
        args = _build_cli_args(
            prompt="Continue the conversation.",
            continue_conversation=True,
        )
        assert "--continue" in args
        assert "--conversation" not in args

    @pytest.mark.asyncio
    async def test_conversation_id_takes_precedence_over_continue(self):
        """When both conversation_id and continue_conversation are set,
        conversation_id wins (per _build_cli_args logic)."""
        args = _build_cli_args(
            prompt="test",
            conversation_id="some-id",
            continue_conversation=True,
        )
        assert "--conversation" in args
        assert "--continue" not in args

    @pytest.mark.asyncio
    async def test_nonexistent_conversation_detected_as_error(self):
        """agy returns exit 0 but warns on missing conversations.
        Our error detection should catch the warning pattern and set status=error."""
        args = _build_cli_args(
            prompt="hello",
            conversation_id="nonexistent_conversation_id_xyz_999",
        )
        result = await execute_cli(args, timeout=30)
        assert result["return_code"] == 0
        assert result["status"] == "error"
        assert "not found" in result["stdout"]


# ---------------------------------------------------------------------------
# 6. Error detection (agy always exits 0)
# ---------------------------------------------------------------------------

class TestErrorDetection:

    @pytest.mark.asyncio
    async def test_successful_command_has_success_status(self):
        args = _build_cli_args(prompt="Say OK")
        result = await execute_cli(args, timeout=60)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_timeout_raises_exception(self):
        """A very short timeout should trigger CLITimeoutError."""
        args = _build_cli_args(
            prompt="Write a 500-word essay about quantum physics."
        )
        with pytest.raises(CLITimeoutError):
            await execute_cli(args, timeout=1)


# ---------------------------------------------------------------------------
# 7. Output sanitization
# ---------------------------------------------------------------------------

class TestOutputSanitization:

    def test_redacts_google_api_key(self):
        raw = "key=AIzaSyA1234567890abcdefghijklmnopqrstuv"
        assert "AIza" not in sanitize_output(raw)

    def test_redacts_openai_key(self):
        raw = "sk-abcdefghijklmnopqrstuvwxyz1234567890ab"
        assert "sk-" not in sanitize_output(raw)

    def test_redacts_bearer_token(self):
        raw = "Authorization: Bearer eyJhbGciOiJSUzI1Ni.payload.sig"
        sanitized = sanitize_output(raw)
        assert "eyJhbGci" not in sanitized
        assert "[REDACTED]" in sanitized

    def test_passthrough_clean_text(self):
        clean = "This is a normal response with no secrets."
        assert sanitize_output(clean) == clean


# ---------------------------------------------------------------------------
# 8. Argument construction edge cases
# ---------------------------------------------------------------------------

class TestBuildCliArgs:

    def test_no_files_no_flags(self):
        args = _build_cli_args(prompt="hello")
        assert args == ["--dangerously-skip-permissions", "--print", "hello"]

    def test_sandbox_and_files(self, sample_file):
        args = _build_cli_args(
            prompt="run this",
            sandbox=True,
            files=[str(sample_file)],
        )
        assert "--sandbox" in args
        assert "--add-dir" in args
        assert "--print" in args

    def test_debug_ignored(self):
        """debug=True should NOT add any flag (agy --debug is a system report)."""
        args = _build_cli_args(prompt="test", debug=True)
        assert "--debug" not in args

    def test_empty_files_list(self):
        args = _build_cli_args(prompt="test", files=[])
        assert "--add-dir" not in args

    def test_prompt_with_special_characters(self):
        prompt = 'Explain "hello world" in Python\'s context'
        args = _build_cli_args(prompt=prompt)
        assert args[-1] == prompt


# ---------------------------------------------------------------------------
# 9. Retry logic
# ---------------------------------------------------------------------------

class TestRetryLogic:

    @pytest.mark.asyncio
    async def test_retry_succeeds_on_valid_command(self):
        """execute_cli_with_retry should succeed on a normal command."""
        args = _build_cli_args(prompt="Reply with OK")
        result = await execute_cli_with_retry(args, timeout=60, max_attempts=2)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_retry_respects_max_attempts_on_timeout(self):
        """With max_attempts=1 and a tiny timeout, should fail immediately."""
        args = _build_cli_args(
            prompt="Write a very long detailed essay about everything."
        )
        with pytest.raises(CLITimeoutError):
            await execute_cli_with_retry(args, timeout=1, max_attempts=1)


# ---------------------------------------------------------------------------
# 10. Full MCP tool round-trip (via mcp_server functions)
# ---------------------------------------------------------------------------

class TestMCPToolRoundTrip:

    @pytest.mark.asyncio
    async def test_gemini_help_tool(self):
        from mcp_server import gemini_help
        result = await gemini_help()
        assert "agy" in result.lower() or "usage" in result.lower()

    @pytest.mark.asyncio
    async def test_gemini_version_tool(self):
        from mcp_server import gemini_version
        result = await gemini_version()
        assert len(result.strip()) > 0

    @pytest.mark.asyncio
    async def test_gemini_prompt_tool(self):
        from mcp_server import gemini_prompt
        raw = await gemini_prompt(prompt="Reply with exactly: TEST_OK")
        result = json.loads(raw)
        assert result["status"] == "success"
        assert result["return_code"] == 0
        assert len(result["stdout"]) > 0

    @pytest.mark.asyncio
    async def test_gemini_prompt_with_file(self, sample_file):
        from mcp_server import gemini_prompt
        raw = await gemini_prompt(
            prompt=f"What function is defined in @{sample_file}? Reply in one word."
        )
        result = json.loads(raw)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_gemini_prompt_exceeds_limit(self):
        from mcp_server import gemini_prompt
        huge = "x" * 200_000
        raw = await gemini_prompt(prompt=huge)
        result = json.loads(raw)
        assert result["status"] == "error"
        assert result["error_code"] == "INPUT_TOO_LARGE"

    @pytest.mark.asyncio
    async def test_gemini_sandbox_tool(self):
        from mcp_server import gemini_sandbox
        raw = await gemini_sandbox(
            prompt="Write a Python one-liner that prints 42, then run it."
        )
        result = json.loads(raw)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_gemini_models_tool(self):
        from mcp_server import gemini_models
        raw = await gemini_models()
        result = json.loads(raw)
        assert result["status"] == "success"
        assert "Antigravity" in result["note"]

    @pytest.mark.asyncio
    async def test_gemini_metrics_tool(self):
        from mcp_server import gemini_metrics
        raw = await gemini_metrics()
        result = json.loads(raw)
        assert result["status"] == "success"
        assert "metrics" in result
        assert result["server_info"]["tools_available"] == 24

    @pytest.mark.asyncio
    async def test_gemini_cache_stats_tool(self):
        from mcp_server import gemini_cache_stats
        raw = await gemini_cache_stats()
        result = json.loads(raw)
        assert result["status"] == "success"
        assert "cache_statistics" in result

    @pytest.mark.asyncio
    async def test_gemini_cli_tool(self):
        from mcp_server import gemini_cli
        raw = await gemini_cli(command="--version")
        result = json.loads(raw)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_gemini_cli_empty_command(self):
        from mcp_server import gemini_cli
        raw = await gemini_cli(command="")
        result = json.loads(raw)
        assert result["status"] == "error"
        assert result["error_code"] == "INVALID_INPUT"


# ---------------------------------------------------------------------------
# 11. Conversation tools round-trip
# ---------------------------------------------------------------------------

class TestConversationToolRoundTrip:

    @pytest.mark.asyncio
    async def test_create_and_metadata_tools(self):
        """Test conversation metadata tools (create, list, stats, clear).
        Note: continue_conversation with a freshly-created UUID will trigger
        agy's 'conversation not found' warning because agy manages .pb files
        independently — the MCP metadata layer and agy storage are separate."""
        from mcp_server import (
            gemini_start_conversation,
            gemini_list_conversations,
            gemini_conversation_stats,
            gemini_clear_conversation,
        )

        # Create metadata entry
        raw = await gemini_start_conversation(
            title="Integration Test",
            tags="test,integration",
            expiration_hours=1,
        )
        result = json.loads(raw)
        assert result["status"] == "success"
        conv_id = result["conversation"]["conversation_id"]
        assert len(conv_id) > 0

        # List should include it
        raw = await gemini_list_conversations(limit=50)
        result = json.loads(raw)
        assert result["status"] == "success"
        conv_ids = [c["conversation_id"] for c in result["conversations"]]
        assert conv_id in conv_ids

        # Stats should work
        raw = await gemini_conversation_stats()
        result = json.loads(raw)
        assert result["status"] == "success"
        assert "statistics" in result

        # Cleanup
        raw = await gemini_clear_conversation(conversation_id=conv_id)
        result = json.loads(raw)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_continue_with_existing_agy_conversation(self):
        """Test continuing an actual agy conversation using --continue."""
        # First, create a real agy conversation via a plain prompt
        args = _build_cli_args(prompt="Say OK")
        result = await execute_cli_with_retry(args, timeout=60)
        assert result["status"] == "success"

        # Now --continue should resume the most recent conversation
        args2 = _build_cli_args(
            prompt="What did you just say?",
            continue_conversation=True,
        )
        result2 = await execute_cli_with_retry(args2, timeout=60)
        assert result2["status"] == "success"
