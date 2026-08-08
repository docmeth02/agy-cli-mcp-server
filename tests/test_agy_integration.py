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

This module requires `agy` on PATH; the autouse fixture below skips it when the
CLI is absent. Other test modules stay runnable without a real installation.
"""
import json
import os
from pathlib import Path

import pytest

import asyncio

from tests.conftest import AGY_AVAILABLE


@pytest.fixture(scope="module", autouse=True)
def _require_agy_for_module():
    """Skip this module when agy is not installed (the rest of the suite runs)."""
    if not AGY_AVAILABLE:
        pytest.skip("Antigravity CLI (agy) not found in PATH")


from modules.utils.cli_utils import (
    execute_cli,
    execute_cli_with_retry,
    _build_cli_args,
    _apply_print_runtime_flags,
    _parse_version,
    extract_file_refs,
    sanitize_output,
    validate_cli_setup,
    validate_model,
    validate_agent,
    add_model_metadata,
    VERSION_CACHE,
    CLITimeoutError,
)
from modules.config.cli_config import (
    CLI_PRINT_TIMEOUT_GRACE,
    get_task_model,
    get_task_timeout,
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
    async def test_nonexistent_conversation_treated_as_new(self):
        """agy >= 1.0.9 silently ignores unknown conversation IDs and runs the
        prompt normally (exit 0, no warning). Older agy emitted a "not found"
        warning; our error patterns still catch that for backward compat, but
        on current agy the result is a successful prompt execution."""
        args = _build_cli_args(
            prompt="Reply with only the word OK",
            conversation_id="nonexistent_conversation_id_xyz_999",
        )
        result = await execute_cli(args, timeout=120)
        assert result["return_code"] == 0
        assert result["status"] == "success"


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
        assert args[0] == "--dangerously-skip-permissions"
        assert args[-2:] == ["--print", "hello"]
        assert "--sandbox" not in args
        assert "--model" not in args

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

    def test_model_flag_injected(self):
        args = _build_cli_args(prompt="hello", model="pro")
        assert "--model" in args
        assert args[args.index("--model") + 1] == "pro"

    def test_model_none_omits_flag(self):
        args = _build_cli_args(prompt="hello", model=None)
        assert "--model" not in args

    def test_model_empty_string_omits_flag(self):
        args = _build_cli_args(prompt="hello", model="")
        assert "--model" not in args

    def test_model_before_print(self):
        args = _build_cli_args(prompt="hello", model="pro")
        assert args.index("--model") < args.index("--print")

    def test_model_with_spaces(self):
        args = _build_cli_args(prompt="hello", model="Gemini 3.5 Flash (Medium)")
        assert args[args.index("--model") + 1] == "Gemini 3.5 Flash (Medium)"

    def test_model_with_sandbox_and_files(self, sample_file):
        args = _build_cli_args(
            prompt="test", sandbox=True, files=[str(sample_file)], model="pro",
        )
        assert "--model" in args
        assert "--sandbox" in args
        assert "--add-dir" in args

    def test_mode_accept_edits_always_injected(self):
        args = _build_cli_args(prompt="hello")
        assert "--mode" in args
        assert args[args.index("--mode") + 1] == "accept-edits"

    def test_mode_before_print(self):
        args = _build_cli_args(prompt="hello")
        assert args.index("--mode") < args.index("--print")

    def test_agent_flag_injected(self):
        args = _build_cli_args(prompt="hello", agent="my-agent")
        assert "--agent" in args
        assert args[args.index("--agent") + 1] == "my-agent"

    def test_agent_none_omits_flag(self):
        args = _build_cli_args(prompt="hello", agent=None)
        assert "--agent" not in args

    def test_project_flag_injected(self):
        args = _build_cli_args(prompt="hello", project="proj-123")
        assert "--project" in args
        assert args[args.index("--project") + 1] == "proj-123"

    def test_new_project_flag(self):
        args = _build_cli_args(prompt="hello", new_project=True)
        assert "--new-project" in args

    def test_project_takes_precedence_over_new_project(self):
        args = _build_cli_args(prompt="hello", project="proj-123", new_project=True)
        assert "--project" in args
        assert "--new-project" not in args

    def test_all_new_flags_together(self, sample_file):
        args = _build_cli_args(
            prompt="test", sandbox=True, files=[str(sample_file)],
            model="pro", agent="reviewer", project="proj-1",
        )
        assert "--mode" in args
        assert "--model" in args
        assert "--agent" in args
        assert "--project" in args
        assert "--sandbox" in args


# ---------------------------------------------------------------------------
# Model configuration resolution
# ---------------------------------------------------------------------------

class TestModelConfig:

    def test_explicit_model_wins(self):
        assert get_task_model("prompt", "claude") == "claude"

    def test_task_default_pro(self):
        # agy 1.1.5 added stable slugs; TASK_MODEL_DEFAULTS uses them because
        # display names track marketing labels and change between releases.
        assert get_task_model("eval_plan") == "gemini-3.1-pro-high"
        assert get_task_model("code_review") == "gemini-3.1-pro-high"
        assert get_task_model("review_code") == "gemini-3.1-pro-high"

    def test_task_default_none_for_lightweight(self):
        assert get_task_model("prompt") is None
        assert get_task_model("summarize") is None
        assert get_task_model("sandbox") is None

    def test_unknown_task_returns_none(self):
        assert get_task_model("nonexistent_task") is None

    def test_explicit_overrides_task_default(self):
        assert get_task_model("eval_plan", "flash") == "flash"


class TestParseVersion:

    def test_bare_version(self):
        assert _parse_version("1.0.5") == (1, 0, 5)

    def test_prefixed_version(self):
        assert _parse_version("agy 1.0.5") == (1, 0, 5)

    def test_version_with_whitespace(self):
        assert _parse_version("  1.0.5\n") == (1, 0, 5)

    def test_garbage_returns_zeros(self):
        assert _parse_version("not-a-version") == (0, 0, 0)

    def test_empty_string(self):
        assert _parse_version("") == (0, 0, 0)


class TestVersionGuard:

    def test_old_version_skips_model(self):
        VERSION_CACHE["version"] = "1.0.4"
        args = _build_cli_args(prompt="hello", model="pro")
        assert "--model" not in args
        VERSION_CACHE.clear()

    def test_valid_version_passes_model(self):
        VERSION_CACHE["version"] = "1.0.5"
        args = _build_cli_args(prompt="hello", model="pro")
        assert "--model" in args
        VERSION_CACHE.clear()

    def test_sync_fetch_on_empty_cache(self):
        VERSION_CACHE.clear()
        args = _build_cli_args(prompt="hello", model="pro")
        assert "--model" in args
        assert "version" in VERSION_CACHE


# ---------------------------------------------------------------------------
# Model selection integration (requires real agy)
# ---------------------------------------------------------------------------

class TestModelIntegration:

    @pytest.mark.asyncio
    async def test_model_flag_works_with_pro_slug(self):
        # Pro (Low) rather than (High): this asserts the slug *form* is accepted,
        # which needs no reasoning depth, and High is the priciest tier.
        args = _build_cli_args(
            prompt="Reply with only the word OK", model="gemini-3.1-pro-low"
        )
        result = await execute_cli_with_retry(args)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_model_flag_works_with_flash_slug(self):
        args = _build_cli_args(
            prompt="Reply with only the word OK", model="gemini-3.5-flash-low"
        )
        result = await execute_cli_with_retry(args)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_model_flag_works_with_display_name(self):
        # Both columns of `agy models` are accepted by --model.
        args = _build_cli_args(
            prompt="Reply with only the word OK", model="Gemini 3.5 Flash (Low)"
        )
        result = await execute_cli_with_retry(args)
        assert result["status"] == "success"

    @pytest.mark.asyncio
    async def test_short_name_is_rejected_by_agy(self):
        # Re-verified on 1.1.11: short names hard-fail rather than falling back.
        args = _build_cli_args(prompt="Reply with only the word OK", model="pro")
        result = await execute_cli_with_retry(args)
        assert result["status"] == "error"


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

    @pytest.mark.asyncio
    async def test_timeout_is_not_retried(self, monkeypatch):
        """A timeout must fail after exactly one attempt, even with max_attempts>1.

        Raising the per-task timeout to 900s makes retrying timeouts dangerous
        (900s x 3), so execute_cli_with_retry must not re-run on CLITimeoutError.
        """
        import modules.utils.cli_utils as cu

        calls = {"n": 0}

        async def fake_execute_cli(args, timeout=None, capture_stderr=True):
            calls["n"] += 1
            raise cu.CLITimeoutError("boom")

        monkeypatch.setattr(cu, "execute_cli", fake_execute_cli)
        with pytest.raises(cu.CLITimeoutError):
            await cu.execute_cli_with_retry(["--version"], timeout=1, max_attempts=3)
        assert calls["n"] == 1


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
        assert "models" in result
        assert len(result["models"]) > 0
        assert "guidance" in result

    @pytest.mark.asyncio
    async def test_gemini_metrics_tool(self):
        from mcp_server import gemini_metrics
        raw = await gemini_metrics()
        result = json.loads(raw)
        assert result["status"] == "success"
        assert "metrics" in result
        assert result["server_info"]["tools_available"] == 27

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


# ---------------------------------------------------------------------------
# 12. Print-mode runtime flags (--print-timeout / --log-file) — pure, no agy
# ---------------------------------------------------------------------------

class TestPrintRuntimeFlags:

    def test_injects_print_timeout_for_print_args(self):
        out = _apply_print_runtime_flags(["--print", "hi"], timeout=300)
        assert "--print-timeout" in out
        idx = out.index("--print-timeout")
        # agy budget sits just above the Python supervisor timeout.
        assert out[idx + 1] == f"{300 + CLI_PRINT_TIMEOUT_GRACE}s"

    def test_print_timeout_tracks_caller_timeout(self):
        out = _apply_print_runtime_flags(["--print", "hi"], timeout=1800)
        idx = out.index("--print-timeout")
        assert out[idx + 1] == f"{1800 + CLI_PRINT_TIMEOUT_GRACE}s"

    def test_preserves_print_payload_as_last_arg(self):
        out = _apply_print_runtime_flags(["--print", "the prompt"], timeout=300)
        assert out[-2:] == ["--print", "the prompt"]

    def test_no_injection_for_non_print_args(self):
        for base in (["--version"], ["help"]):
            assert _apply_print_runtime_flags(list(base), timeout=300) == base

    def test_does_not_override_existing_print_timeout(self):
        base = ["--print-timeout", "5s", "--print", "hi"]
        out = _apply_print_runtime_flags(list(base), timeout=300)
        assert out.count("--print-timeout") == 1
        assert "5s" in out

    def test_does_not_duplicate_joined_form_flags(self):
        # `--flag=val` form must be recognised as already-present (no dup).
        base = ["--print-timeout=5s", "--print", "hi"]
        out = _apply_print_runtime_flags(list(base), timeout=300)
        assert sum(a.startswith("--print-timeout") for a in out) == 1

    def test_triggers_for_prompt_alias(self):
        out = _apply_print_runtime_flags(["--prompt", "hi"], timeout=300)
        assert "--print-timeout" in out

    def test_prompt_payload_not_treated_as_flag(self):
        # A prompt whose text starts with a flag string must NOT suppress
        # injection (the payload token is excluded from flag detection).
        out = _apply_print_runtime_flags(
            ["--print", "--print-timeout=5s"], timeout=300
        )
        # Injected flag present AND the prompt payload preserved verbatim.
        assert out[:2] == ["--print-timeout", f"{300 + CLI_PRINT_TIMEOUT_GRACE}s"]
        assert out[-2:] == ["--print", "--print-timeout=5s"]

    def test_duplicate_print_flags_payloads_all_excluded(self):
        # Every print-flag payload is excluded from detection, not just the
        # first — a later payload starting with a flag string must not suppress.
        out = _apply_print_runtime_flags(
            ["--print", "one", "--print", "--print-timeout=5s"], timeout=300
        )
        assert out[:2] == ["--print-timeout", f"{300 + CLI_PRINT_TIMEOUT_GRACE}s"]

    def test_log_file_opt_in(self, monkeypatch):
        # Default (unset): no --log-file injected.
        monkeypatch.setattr("modules.utils.cli_utils.CLI_LOG_FILE", "")
        out = _apply_print_runtime_flags(["--print", "hi"], timeout=300)
        assert "--log-file" not in out
        # Configured: injected with the given path.
        monkeypatch.setattr("modules.utils.cli_utils.CLI_LOG_FILE", "/tmp/agy.log")
        out = _apply_print_runtime_flags(["--print", "hi"], timeout=300)
        assert out[out.index("--log-file") + 1] == "/tmp/agy.log"


class TestSubprocessEnvironment:
    """Verify the env handed to the agy subprocess (mocked — no real agy)."""

    @pytest.mark.asyncio
    async def test_env_isolation_and_preservation(self, monkeypatch):
        captured = {}

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"OK\n", b"")

            def kill(self):
                pass

            async def wait(self):
                return 0

        async def _fake_exec(*args, **kwargs):
            captured["args"] = args
            captured["env"] = kwargs.get("env")
            return _FakeProc()

        monkeypatch.setenv("ANTIGRAVITY_LS_ADDRESS", "localhost:9999")
        # Even an explicit disable in the launch env must be force-overridden.
        monkeypatch.setenv("AGY_CLI_HIDE_ACCOUNT_INFO", "0")
        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)

        result = await execute_cli(["--print", "hi"], timeout=300)
        assert result["status"] == "success"

        env = captured["env"]
        # IDE language-server address stripped so agy uses its own backend.
        assert "ANTIGRAVITY_LS_ADDRESS" not in env
        # Account/credits header suppressed so it can't leak into stdout.
        assert env["AGY_CLI_HIDE_ACCOUNT_INFO"] == "1"
        # Critical user-space vars preserved so agy resolves ~/.gemini config.
        assert env.get("HOME") == os.environ.get("HOME")
        assert env.get("PATH") == os.environ.get("PATH")
        # --print-timeout injected into the actual argv passed to the subprocess.
        assert "--print-timeout" in captured["args"]


# ---------------------------------------------------------------------------
# 13. Per-task timeout resolution (no agy needed beyond the session gate)
# ---------------------------------------------------------------------------

class TestTaskTimeout:

    def test_heavy_task_raised_above_default(self):
        assert get_task_timeout("verify_solution") == 900
        assert get_task_timeout("code_review") == 900
        assert get_task_timeout("eval_plan") == 600

    def test_unknown_task_inherits_default(self):
        from modules.config.cli_config import CLI_TIMEOUT
        assert get_task_timeout("nonexistent_task") == CLI_TIMEOUT

    def test_explicit_wins(self):
        assert get_task_timeout("verify_solution", 42) == 42

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CLI_TIMEOUT_VERIFY_SOLUTION", "123")
        assert get_task_timeout("verify_solution") == 123

    def test_env_override_invalid_falls_through(self, monkeypatch):
        monkeypatch.setenv("CLI_TIMEOUT_EVAL_PLAN", "not-a-number")
        assert get_task_timeout("eval_plan") == 600


# ---------------------------------------------------------------------------
# 14. Model validation warnings (deterministic — version/models mocked)
# ---------------------------------------------------------------------------

class TestModelValidation:

    @pytest.mark.asyncio
    async def test_none_model_no_metadata(self):
        assert await validate_model(None) == {}
        assert await validate_model("") == {}

    @pytest.mark.asyncio
    async def test_short_names_accepted_below_1_1_4(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.0.8")

        async def _boom():
            raise AssertionError("discovery should not be called for short names")

        monkeypatch.setattr(cu, "get_available_models", _boom)
        assert await validate_model("pro") == {}
        assert await validate_model("FLASH") == {}
        assert await validate_model("claude") == {}

    @pytest.mark.asyncio
    async def test_full_name_in_list_ok(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.0.8")

        async def _models():
            return [
                {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"},
                {"slug": "gemini-3.5-flash-medium", "display_name": "Gemini 3.5 Flash (Medium)"},
            ]

        monkeypatch.setattr(cu, "get_available_models", _models)
        assert await validate_model("Gemini 3.1 Pro (High)") == {}

    @pytest.mark.asyncio
    async def test_unknown_model_warns(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.0.8")

        async def _models():
            return [
                {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"}
            ]

        monkeypatch.setattr(cu, "get_available_models", _models)
        meta = await validate_model("typo-model")
        assert "warning" in meta and "not recognized" in meta["warning"]

    @pytest.mark.asyncio
    async def test_discovery_failure_marks_unverified(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.0.8")

        async def _models():
            return []

        monkeypatch.setattr(cu, "get_available_models", _models)
        assert await validate_model("future-model") == {"model_validation": "unverified"}

    @pytest.mark.asyncio
    async def test_old_version_warns_not_applied(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.0.4")
        meta = await validate_model("pro")
        assert "warning" in meta and "not applied" in meta["warning"]

    def test_add_model_metadata_merges_and_noop(self):
        d = {"status": "success"}
        assert add_model_metadata(d, {"warning": "x"}) is d
        assert d["warning"] == "x"
        d2 = {"status": "success"}
        add_model_metadata(d2, {})
        assert "warning" not in d2

    def test_add_model_metadata_preserves_existing_warning(self):
        # A model warning must not clobber a pre-existing warning (e.g. a
        # conversation "metadata update failed" notice) — they concatenate.
        d = {"status": "success", "warning": "metadata update failed"}
        add_model_metadata(d, {"warning": "model 'bogus' not recognized"})
        assert "metadata update failed" in d["warning"]
        assert "not recognized" in d["warning"]

    @pytest.mark.asyncio
    async def test_full_name_match_is_case_insensitive(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.0.8")

        async def _models():
            return [
                {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"}
            ]

        monkeypatch.setattr(cu, "get_available_models", _models)
        assert await validate_model("gemini 3.1 pro (high)") == {}


# ---------------------------------------------------------------------------
# 15. Agent validation (deterministic — version/agents mocked)
# ---------------------------------------------------------------------------

class TestAgentValidation:

    @pytest.mark.asyncio
    async def test_none_agent_no_metadata(self):
        assert await validate_agent(None) == {}
        assert await validate_agent("") == {}

    @pytest.mark.asyncio
    async def test_old_version_warns_not_applied(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.0")
        meta = await validate_agent("my-agent")
        assert "warning" in meta and "not applied" in meta["warning"]

    @pytest.mark.asyncio
    async def test_unknown_agent_warns(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.1")

        async def _agents():
            return ["reviewer", "coder"]

        monkeypatch.setattr(cu, "get_available_agents", _agents)
        meta = await validate_agent("nonexistent")
        assert "warning" in meta and "not recognized" in meta["warning"]

    @pytest.mark.asyncio
    async def test_known_agent_no_warning(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.1")

        async def _agents():
            return ["reviewer"]

        monkeypatch.setattr(cu, "get_available_agents", _agents)
        assert await validate_agent("reviewer") == {}

    @pytest.mark.asyncio
    async def test_empty_list_marks_unverified(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.1")

        async def _agents():
            return []

        monkeypatch.setattr(cu, "get_available_agents", _agents)
        assert await validate_agent("x") == {"agent_validation": "unverified"}


# ---------------------------------------------------------------------------
# 16. Error detection prefers stderr on non-zero exit (agy >= 1.1.1)
# ---------------------------------------------------------------------------

class TestErrorDetectionStderr:

    @pytest.mark.asyncio
    async def test_nonzero_exit_surfaces_stderr(self, monkeypatch):
        """When agy returns non-zero exit + stderr (1.1.1+), the error result
        should carry stderr in stdout for callers that key off stdout."""
        import modules.utils.cli_utils as cu

        class _FakeProc:
            returncode = 1

            async def communicate(self):
                return (b"", b"server error: model not available\n")

            def kill(self):
                pass

            async def wait(self):
                return 1

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        result = await execute_cli(["--print", "hi"], timeout=10)
        assert result["status"] == "error"
        assert "server error" in result["stdout"]

    @pytest.mark.asyncio
    async def test_zero_exit_with_stdout_error_still_detected(self, monkeypatch):
        """Backward compat: exit 0 + stdout error pattern → error result."""
        import modules.utils.cli_utils as cu

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return (b"Error: something went wrong\n", b"")

            def kill(self):
                pass

            async def wait(self):
                return 0

        async def _fake_exec(*args, **kwargs):
            return _FakeProc()

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
        result = await execute_cli(["--print", "hi"], timeout=10)
        assert result["status"] == "error"
        assert "something went wrong" in result["stdout"]


# ---------------------------------------------------------------------------
# 17. Sandbox scope (real agy; opt-in via RUN_SANDBOX_ISOLATION=1)
# ---------------------------------------------------------------------------
# NOTE: agy's --sandbox enables TERMINAL command restrictions, NOT a filesystem
# jail. With our always-on --dangerously-skip-permissions, the agent's file
# tools can still write outside the workspace (verified: a --sandbox print run
# created a file at an absolute tmp path). So there is intentionally no test
# asserting filesystem isolation — it would encode a guarantee agy does not make.
# The 1.0.6 print-mode propagation fix is covered by TestSandboxMode
# (sandbox flag runs and returns success in --print mode).

@pytest.mark.sandbox_isolation
@pytest.mark.skipif(
    os.getenv("RUN_SANDBOX_ISOLATION") != "1",
    reason="slow real-agy behavioral check; set RUN_SANDBOX_ISOLATION=1 to run",
)
class TestSandboxScope:
    """Documents agy --sandbox scope: terminal restrictions, not a file jail."""

    @pytest.mark.asyncio
    async def test_sandbox_does_not_jail_filesystem(self, tmp_path):
        # Confirms (and pins) the real boundary: under --sandbox +
        # --dangerously-skip-permissions, an out-of-workspace write is NOT blocked.
        outside = tmp_path / "escaped.txt"
        prompt = (
            f"Create the file {outside} with the exact contents ESCAPED, using "
            f"any file tool available."
        )
        args = _build_cli_args(prompt=prompt, sandbox=True)
        await execute_cli_with_retry(args, timeout=get_task_timeout("sandbox"))
        assert outside.exists(), (
            "agy --sandbox unexpectedly blocked an out-of-workspace file write; "
            "if agy added a filesystem jail, update the docs/security model."
        )
