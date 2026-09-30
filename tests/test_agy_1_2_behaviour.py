"""
Unit tests for behaviour introduced between agy 1.1.12 and 1.2.14.

Covers the structured AGY_ERROR stderr record (1.2.6), agy's own retry verdict
(1.2.13), `denied_actions` passthrough (1.1.27), the unknown-conversation
warning moving to stderr (1.1.12), `--effort max` (1.2.11) and tier-free base
model slugs. Each expectation was measured against agy 1.2.14 where it could be
triggered on demand; AGY_ERROR could not, so its shape follows the binary's
JSON tags and the parser is exercised defensively.

No real `agy` installation required — the subprocess layer is stubbed.
"""
import asyncio
import json

import pytest

import modules.utils.cli_utils as cu
from modules.utils.cli_utils import (
    CLIRateLimitError,
    _adapt_json_envelope,
    _build_cli_args,
    _parse_agy_error,
    base_model_slugs,
    execute_cli,
    validate_effort,
    validate_model,
)
from tests.test_json_transport import (
    ERROR_ENVELOPE,
    SUCCESS_ENVELOPE,
    stub_subprocess,
)

ROSTER_1_2_14 = [
    {"slug": f"gemini-3.{v}-flash-{t}", "display_name": f"Gemini 3.{v} Flash ({t.title()})"}
    for v in (8, 7, 6) for t in ("high", "medium", "low")
] + [
    {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"},
    {"slug": "gemini-3.1-pro-low", "display_name": "Gemini 3.1 Pro (Low)"},
    {"slug": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6 (Thinking)"},
    {"slug": "claude-opus-4-6-thinking", "display_name": "Claude Opus 4.6 (Thinking)"},
    {"slug": "gpt-oss-120b-medium", "display_name": "GPT-OSS 120B (Medium)"},
]


@pytest.fixture
def agy_1_2_14(monkeypatch):
    monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.2.14")
    monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")

    async def _models():
        return ROSTER_1_2_14

    monkeypatch.setattr(cu, "get_available_models", _models)


# ---------------------------------------------------------------------------
# AGY_ERROR parsing
# ---------------------------------------------------------------------------

class TestParseAgyError:

    def test_absent(self):
        assert _parse_agy_error("") is None
        assert _parse_agy_error("error: something else\n") is None

    def test_parses_record(self):
        rec = {"short_error": "quota", "status": "RESOURCE_EXHAUSTED",
               "code": 429, "retryable": False, "error_id": "abc"}
        assert _parse_agy_error(f"noise\nAGY_ERROR: {json.dumps(rec)}\n") == rec

    def test_last_record_wins(self):
        stderr = ('AGY_ERROR: {"short_error": "first"}\n'
                  'AGY_ERROR: {"short_error": "last"}\n')
        assert _parse_agy_error(stderr)["short_error"] == "last"

    def test_garbage_is_ignored_not_raised(self):
        assert _parse_agy_error("AGY_ERROR: {not json\n") is None
        assert _parse_agy_error("AGY_ERROR: [1, 2]\n") is None

    def test_deep_nesting_does_not_raise(self):
        # _sanitize_tree recurses and fails at about half the depth json.loads
        # accepts; that RecursionError must not escape and fail a good run.
        for depth in (300, 600, 900, 2000):
            line = "AGY_ERROR: " + '{"a":' * depth + "1" + "}" * depth
            result = _parse_agy_error(line)  # must not raise
            assert result is None or result == json.loads(line[len("AGY_ERROR: "):])

    def test_deep_nesting_keeps_successful_result(self, agy_1_2_14, monkeypatch):
        depth = 600
        stderr = ("AGY_ERROR: " + '{"a":' * depth + "1" + "}" * depth + "\n").encode()
        stub_subprocess(monkeypatch, json.dumps(SUCCESS_ENVELOPE).encode(), stderr=stderr, rc=0)
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "success"
        assert r["conversation_id"] == SUCCESS_ENVELOPE["conversation_id"]

    def test_oversized_record_is_skipped(self):
        line = 'AGY_ERROR: {"short_error": "' + "x" * (70 * 1024) + '"}'
        assert _parse_agy_error(line) is None

    def test_falls_back_past_unusable_candidates(self):
        stderr = ('AGY_ERROR: {"short_error": "good"}\n'
                  'AGY_ERROR: {truncated\n'
                  'AGY_ERROR: "' + "x" * (70 * 1024) + '"\n')
        assert _parse_agy_error(stderr) == {"short_error": "good"}

    def test_log_prefixed_line_is_parsed(self):
        rec = _parse_agy_error('2026/09/30 10:00:00 AGY_ERROR: {"retryable": true}\n')
        assert rec == {"retryable": True}

    def test_record_is_sanitized(self):
        key = "AIza" + "A" * 35
        rec = _parse_agy_error(f'AGY_ERROR: {{"short_error": "bad key {key}"}}\n')
        assert key not in json.dumps(rec)


# ---------------------------------------------------------------------------
# execute_cli integration of AGY_ERROR + retry verdict
# ---------------------------------------------------------------------------

class TestAgyErrorInExecuteCli:

    def test_attached_to_json_result(self, agy_1_2_14, monkeypatch):
        rec = {"short_error": "model API failure", "retryable": True}
        stub_subprocess(monkeypatch, json.dumps(ERROR_ENVELOPE).encode(),
                        stderr=f"AGY_ERROR: {json.dumps(rec)}\n".encode(), rc=3)
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "error"
        assert r["return_code"] == 3
        assert r["agy_error"] == rec

    def test_attached_to_text_result_and_exit_3_is_error(self, agy_1_2_14, monkeypatch):
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "text")
        rec = {"short_error": "stream dropped"}
        stub_subprocess(monkeypatch, b"partial answer",
                        stderr=f"AGY_ERROR: {json.dumps(rec)}\n".encode(), rc=3)
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "error"
        assert r["agy_error"] == rec

    def test_rate_limit_carries_agy_retryable_verdict(self, agy_1_2_14, monkeypatch):
        env = dict(ERROR_ENVELOPE, error="quota exhausted: daily cap")
        stub_subprocess(monkeypatch, json.dumps(env).encode(),
                        stderr=b'AGY_ERROR: {"short_error": "quota", "retryable": false}\n',
                        rc=3)
        with pytest.raises(CLIRateLimitError) as exc:
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert exc.value.retryable is False
        assert exc.value.work_done is False

    def test_non_bool_retryable_is_ignored(self, agy_1_2_14, monkeypatch):
        env = dict(ERROR_ENVELOPE, error="quota")
        stub_subprocess(monkeypatch, json.dumps(env).encode(),
                        stderr=b'AGY_ERROR: {"retryable": "no"}\n', rc=3)
        with pytest.raises(CLIRateLimitError) as exc:
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert exc.value.retryable is None


class TestRetryVerdict:

    def _count(self, monkeypatch, exc):
        calls = {"n": 0}

        async def _fake(args, timeout=None):
            calls["n"] += 1
            raise exc

        monkeypatch.setattr(cu, "execute_cli", _fake)
        real_sleep = asyncio.sleep

        async def _no_delay(_s):
            await real_sleep(0)

        monkeypatch.setattr(cu.asyncio, "sleep", _no_delay)
        return calls

    def test_agy_non_retryable_vetoes_retry(self, monkeypatch):
        calls = self._count(monkeypatch, CLIRateLimitError(
            "cap", work_done=False, retryable=False))
        with pytest.raises(CLIRateLimitError):
            asyncio.run(cu.execute_cli_with_retry(
                ["--print", "x"], mutating=False, max_attempts=3))
        assert calls["n"] == 1

    def test_agy_retryable_still_retries(self, monkeypatch):
        calls = self._count(monkeypatch, CLIRateLimitError(
            "rpm", work_done=False, retryable=True))
        with pytest.raises(CLIRateLimitError):
            asyncio.run(cu.execute_cli_with_retry(
                ["--print", "x"], mutating=False, max_attempts=3))
        assert calls["n"] == 3

    def test_retryable_true_does_not_override_work_done(self, monkeypatch):
        calls = self._count(monkeypatch, CLIRateLimitError(
            "rpm", work_done=True, retryable=True))
        with pytest.raises(CLIRateLimitError):
            asyncio.run(cu.execute_cli_with_retry(
                ["--print", "x"], mutating=False, max_attempts=3))
        assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Envelope passthrough + conversation warning
# ---------------------------------------------------------------------------

class TestDeniedActions:

    def test_passthrough(self):
        env = dict(SUCCESS_ENVELOPE, denied_actions=[{"tool": "run_command"}])
        r = _adapt_json_envelope(env, 0, 1.0, "")
        assert r["denied_actions"] == [{"tool": "run_command"}]

    def test_absent_or_empty_not_added(self):
        assert "denied_actions" not in _adapt_json_envelope(dict(SUCCESS_ENVELOPE), 0, 1.0, "")
        for empty in (None, []):
            env = dict(SUCCESS_ENVELOPE, denied_actions=empty)
            assert "denied_actions" not in _adapt_json_envelope(env, 0, 1.0, "")


class TestConversationNotFoundOnStderr:
    # Measured on 1.2.14: exit 0, answer on stdout, and on stderr
    # `warning: conversation "<id>" not found` — the history was NOT used.

    def test_text_transport_flags_it(self, agy_1_2_14, monkeypatch):
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "text")
        stub_subprocess(
            monkeypatch, b"OK\n",
            stderr=b'warning: conversation "11111111-2222-3333-4444-555555555555" not found\n',
            rc=0,
        )
        r = asyncio.run(execute_cli(["--conversation", "x", "--print", "hi"], timeout=10))
        assert r["status"] == "error"
        assert "NEW conversation" in r["error"]
        assert r["stdout"] == "OK\n"  # the answer is still delivered

    def test_legacy_stdout_form_gets_the_same_error(self, agy_1_2_14, monkeypatch):
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "text")
        stub_subprocess(monkeypatch,
                        b'Warning: conversation "abc" not found\nOK\n', rc=0)
        r = asyncio.run(execute_cli(["--conversation", "abc", "--print", "hi"], timeout=10))
        assert r["status"] == "error"
        assert "NEW conversation" in r["error"]

    def test_subcommand_is_not_scanned(self, agy_1_2_14, monkeypatch):
        stub_subprocess(monkeypatch, b"list\n",
                        stderr=b'warning: conversation "a" not found\n', rc=0)
        r = asyncio.run(execute_cli(["models"], timeout=10))
        assert r["status"] == "success"


# ---------------------------------------------------------------------------
# Effort `max` and base slugs
# ---------------------------------------------------------------------------

class TestEffortMax:

    def test_max_passed_on_1_2_11_plus(self, agy_1_2_14):
        args = _build_cli_args(prompt="hi", effort="max")
        assert args[args.index("--effort") + 1] == "max"

    def test_max_skipped_below_1_2_11(self, monkeypatch):
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.11")
        args = _build_cli_args(prompt="hi", effort="max")
        assert "--effort" not in args
        # The other levels keep their 1.1.10 floor.
        assert "--effort" in _build_cli_args(prompt="hi", effort="high")

    def test_validate_effort_warns_below_floor(self, monkeypatch):
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.11")
        meta = asyncio.run(validate_effort("max"))
        assert "1.2.11" in meta["warning"]

    def test_max_on_family_without_max_warns(self, agy_1_2_14):
        # Measured: "gemini-3.8-flash has no \"max\" effort (available: ...)".
        meta = asyncio.run(validate_effort("max", "gemini-3.8-flash"))
        assert "no 'max' effort" in meta["warning"]
        assert "low, medium, high" in meta["warning"]

    def test_max_accepted_where_family_offers_it(self, agy_1_2_14, monkeypatch):
        roster = [{"slug": f"future-model-{t}", "display_name": f"F ({t})"}
                  for t in ("high", "max")]

        async def _models():
            return roster
        monkeypatch.setattr(cu, "get_available_models", _models)
        assert asyncio.run(validate_effort("max", "future-model")) == {}


class TestEffortAgainstRoster:

    def test_base_slug_valid_tier_ok(self, agy_1_2_14):
        assert asyncio.run(validate_effort("low", "gemini-3.1-pro")) == {}
        assert asyncio.run(validate_effort("medium", "gemini-3.8-flash")) == {}

    def test_pro_has_no_medium(self, agy_1_2_14):
        meta = asyncio.run(validate_effort("medium", "gemini-3.1-pro"))
        assert "available: low, high" in meta["warning"]

    def test_untiered_model_rejects_effort(self, agy_1_2_14):
        # Measured: '--effort is not supported for model "claude-opus-4-6-thinking"'.
        for m in ("claude-opus-4-6-thinking", "Claude Sonnet 4.6 (Thinking)"):
            meta = asyncio.run(validate_effort("high", m))
            assert "does not accept an effort" in meta["warning"]

    def test_no_roster_stays_quiet(self, agy_1_2_14, monkeypatch):
        async def _none():
            return []
        monkeypatch.setattr(cu, "get_available_models", _none)
        assert asyncio.run(validate_effort("medium", "gemini-3.1-pro")) == {}
        # The tier conflict needs no roster.
        assert "rejects" in asyncio.run(
            validate_effort("low", "gemini-3.8-flash-high"))["warning"]


class TestTierConflict:
    # Measured on 1.2.14: `--model gemini-3.8-flash-high --effort low` exits 1
    # with "conflicts with --effort=low"; the same tier on both sides succeeds.

    def test_conflict_names_the_base_slug(self, agy_1_2_14):
        meta = asyncio.run(validate_effort("low", "gemini-3.8-flash-high"))
        assert "rejects" in meta["warning"]
        assert "'gemini-3.8-flash'" in meta["warning"]

    def test_same_tier_is_fine(self, agy_1_2_14):
        assert asyncio.run(validate_effort("high", "gemini-3.8-flash-high")) == {}

    def test_display_name_conflict_resolves_to_base_slug(self, agy_1_2_14):
        meta = asyncio.run(validate_effort("high", "Gemini 3.1 Pro (Low)"))
        assert "rejects" in meta["warning"]
        assert "'gemini-3.1-pro'" in meta["warning"]

    def test_never_suggests_a_nonexistent_base(self, agy_1_2_14):
        # gpt-oss-120b is not a family: agy rejects both the slug and --effort.
        meta = asyncio.run(validate_effort("low", "gpt-oss-120b-medium"))
        assert "gpt-oss-120b'" not in meta["warning"]
        assert "Drop effort" in meta["warning"]

    def test_never_suggests_a_missing_tier(self, agy_1_2_14):
        meta = asyncio.run(validate_effort("medium", "gemini-3.1-pro-high"))
        assert "Pass the base slug" not in meta["warning"]
        assert "available: low, high" in meta["warning"]
        meta = asyncio.run(validate_effort("max", "gemini-3.8-flash-high"))
        assert "Pass the base slug" not in meta["warning"]


class TestBaseSlugs:

    def test_derived_only_for_multi_tier_families(self):
        bases = base_model_slugs(ROSTER_1_2_14)
        assert {"gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash",
                "gemini-3.1-pro"} <= bases
        # Single-tier model: agy rejects --effort for it, so no base.
        assert "gpt-oss-120b" not in bases
        assert not any(b.startswith("claude") for b in bases)

    def test_validate_model_accepts_base_slug_with_effort(self, agy_1_2_14):
        assert asyncio.run(validate_model("gemini-3.8-flash", "low")) == {}
        assert asyncio.run(validate_model("GEMINI-3.1-PRO", "high")) == {}

    def test_base_slug_without_effort_warns(self, agy_1_2_14):
        # Measured: "--model gemini-3.8-flash requires --effort". Tools other
        # than gemini_prompt / gemini_sandbox cannot pass effort at all.
        meta = asyncio.run(validate_model("gemini-3.8-flash"))
        assert "gemini-3.8-flash-high" in meta["warning"]

    def test_base_slug_suggestion_uses_an_offered_tier(self, agy_1_2_14, monkeypatch):
        roster = [{"slug": f"fam-{t}", "display_name": f"Fam ({t})"}
                  for t in ("low", "medium")]

        async def _models():
            return roster
        monkeypatch.setattr(cu, "get_available_models", _models)
        meta = asyncio.run(validate_model("fam"))
        assert "'fam-medium'" in meta["warning"]

    def test_removed_model_is_flagged(self, agy_1_2_14):
        meta = asyncio.run(validate_model("gemini-3.5-flash-low"))
        assert "not recognized" in meta["warning"]


# ---------------------------------------------------------------------------
# Timeout resolution (unit-level: runs without agy)
# ---------------------------------------------------------------------------

class TestTaskTimeoutFloor:
    # CLI_TIMEOUT went 300 -> 900 because the old default produced the
    # reported "5 minute timeouts"; a task default may only raise a tool.

    def test_default_is_900(self):
        import os
        import modules.config.cli_config as cfg
        if os.getenv("CLI_TIMEOUT") or os.getenv("GEMINI_TIMEOUT"):
            pytest.skip("CLI_TIMEOUT/GEMINI_TIMEOUT set in the environment")
        assert cfg.CLI_TIMEOUT == 900

    def test_task_default_never_below_global(self, monkeypatch):
        import modules.config.cli_config as cfg
        monkeypatch.setattr(cfg, "CLI_TIMEOUT", 1200)
        assert cfg.get_task_timeout("eval_plan") == 1200
        assert cfg.get_task_timeout("prompt") == 1200
        monkeypatch.setattr(cfg, "CLI_TIMEOUT", 60)
        assert cfg.get_task_timeout("eval_plan") == 600
        assert cfg.get_task_timeout("prompt") == 60

    def test_explicit_and_env_still_bypass_the_floor(self, monkeypatch):
        import modules.config.cli_config as cfg
        monkeypatch.setattr(cfg, "CLI_TIMEOUT", 900)
        assert cfg.get_task_timeout("eval_plan", 42) == 42
        monkeypatch.setenv("CLI_TIMEOUT_EVAL_PLAN", "120")
        assert cfg.get_task_timeout("eval_plan") == 120
