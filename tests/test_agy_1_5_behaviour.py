"""
Unit tests for behaviour introduced between agy 1.2.15 and 1.5.3.

Each expectation was measured against agy 1.5.3 where it could be triggered on
demand (the envelopes and stderr lines below are copied from real runs):

  - the roster drop of Gemini 3.1 Pro, and the runtime fallback for a shipped
    default agy no longer offers;
  - `--print-timeout` ending a turn cleanly (1.5.0) with `finished_reason`
    (1.4.11), and the timeout layering built on it;
  - `--effort xhigh`, base slugs without effort (1.3.4), agy rejecting an
    unknown `--agent` itself (1.3.4);
  - `--mode plan` holding back writes, used for enforced read-only runs;
  - `--settings '{"useG1Credits": ...}'` for the AI-credit pin.

No real `agy` installation required — the subprocess layer is stubbed.
"""
import json

import pytest

import modules.utils.cli_utils as cu
from modules.utils.cli_utils import (
    _adapt_json_envelope,
    _agy_print_timeout,
    _apply_print_runtime_flags,
    _build_cli_args,
    _pinned_tier,
    drop_unavailable_default,
    execute_cli,
    resolve_readonly_enforcement,
    resolve_task_model,
    validate_agent,
    validate_effort,
    validate_model,
)
from tests.test_json_transport import stub_subprocess

# `agy models` on 1.5.3.
ROSTER_1_5_3 = [
    {"slug": f"gemini-3.8-flash-{t}", "display_name": f"Gemini 3.8 Flash ({t.title()})"}
    for t in ("high", "medium", "low")
] + [
    {"slug": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6 (Thinking)"},
    {"slug": "claude-opus-4-6-thinking", "display_name": "Claude Opus 4.6 (Thinking)"},
    {"slug": "gpt-oss-120b-medium", "display_name": "GPT-OSS 120B (Medium)"},
]

# `--print-timeout 4s` on a long prompt, agy 1.5.3 (exit 3).
PRINT_TIMEOUT_ENVELOPE = {
    "conversation_id": "0b1e8f3c-58a4-4c3e-9d0e-2f7a6c1d9e11",
    "status": "ERROR",
    "response": "# Spanning the Void: A Cultural, Architectural, and Technological History",
    "error": "print timeout after 4s with the turn still in progress; the response is partial",
    "duration_seconds": 4.2,
    "num_turns": 1,
    "usage": {"input_tokens": 16262, "output_tokens": 412, "total_tokens": 16674},
    "finished_reason": "print_timeout",
}
PRINT_TIMEOUT_STDERR = (
    "[agy] print timeout after 4s with turn in progress; returning partial output\n"
    "error: print timeout after 4s with the turn still in progress; the response is partial\n"
    'AGY_ERROR: {"short_error":"print timeout after 4s with the turn still in progress; '
    'the response is partial","status":"DEADLINE_EXCEEDED","retryable":false,'
    '"reason":"print_timeout"}\n'
)


def _pin_version(monkeypatch, version: str):
    monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: version)


@pytest.fixture
def agy_1_5_3(monkeypatch):
    _pin_version(monkeypatch, "1.5.3")
    monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
    monkeypatch.setattr(cu, "CLI_AI_CREDITS", "inherit")

    async def _models():
        return ROSTER_1_5_3

    monkeypatch.setattr(cu, "get_available_models", _models)


def _flag_value(args: list[str], flag: str):
    return args[args.index(flag) + 1] if flag in args else None


# ---------------------------------------------------------------------------
# Print timeout layering
# ---------------------------------------------------------------------------

class TestPrintTimeoutLayering:

    def test_agy_stops_first_on_1_5_0_plus(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", 30)
        assert _agy_print_timeout(900, (1, 5, 0)) == 870
        assert _agy_print_timeout(900, (1, 5, 3)) == 870

    def test_python_stops_first_below_1_5_0(self, monkeypatch):
        # Before 1.5.0 a cut-short print run could exit 0, so a partial answer
        # would pass as complete if agy fired first.
        monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", 30)
        assert _agy_print_timeout(900, (1, 4, 12)) == 930

    def test_unknown_version_keeps_python_first(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", 30)
        assert _agy_print_timeout(900, (0, 0, 0)) == 930

    def test_short_budget_keeps_most_of_its_time(self, monkeypatch):
        # A fixed 30s gap halved a 60s budget and cut off a normal 30s run.
        monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", 30)
        assert _agy_print_timeout(60, (1, 5, 3)) == 54
        assert _agy_print_timeout(30, (1, 5, 3)) == 27
        assert _agy_print_timeout(120, (1, 5, 3)) == 108

    def test_gap_never_exceeds_grace(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", 10)
        assert _agy_print_timeout(900, (1, 5, 3)) == 890

    def test_non_positive_grace_keeps_agy_below_the_supervisor(self, monkeypatch):
        # A grace of 0 or less must not hand agy the full budget (or more).
        for grace in (0, -5):
            monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", grace)
            assert _agy_print_timeout(900, (1, 5, 3)) == 899

    def test_never_zero_or_negative(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", 30)
        assert _agy_print_timeout(3, (1, 5, 3)) == 1
        assert _agy_print_timeout(1, (1, 5, 3)) == 1

    def test_injected_value_follows_the_version(self, agy_1_5_3, monkeypatch):
        monkeypatch.setattr(cu, "CLI_PRINT_TIMEOUT_GRACE", 30)
        args, _ = _apply_print_runtime_flags(["--print", "hi"], 900)
        assert _flag_value(args, "--print-timeout") == "870s"
        _pin_version(monkeypatch, "1.4.12")
        args, _ = _apply_print_runtime_flags(["--print", "hi"], 900)
        assert _flag_value(args, "--print-timeout") == "930s"


class TestFinishedReason:

    def test_print_timeout_keeps_partial_answer(self):
        r = _adapt_json_envelope(PRINT_TIMEOUT_ENVELOPE, 3, 4.3, "")
        assert r["status"] == "error"
        assert r["finished_reason"] == "print_timeout"
        assert r["error_code"] == "TIMEOUT"
        assert r["partial"] is True
        assert r["stdout"].startswith("# Spanning the Void")
        assert "partial" in r["error"]

    def test_timeout_with_empty_response_is_not_partial(self):
        env = dict(PRINT_TIMEOUT_ENVELOPE, response="")
        r = _adapt_json_envelope(env, 3, 4.3, "")
        assert r["partial"] is False
        assert r["error_code"] == "TIMEOUT"

    def test_other_reasons_pass_through_without_timeout_code(self):
        # Every pre-turn failure on 1.5.3 carries finished_reason "error".
        env = {"status": "ERROR", "error": "unknown agent \"x\"", "response": "",
               "num_turns": 0, "finished_reason": "error"}
        r = _adapt_json_envelope(env, 1, 0.1, "")
        assert r["finished_reason"] == "error"
        assert "error_code" not in r and "partial" not in r

    def test_normal_completion_has_no_reason(self):
        env = {"status": "SUCCESS", "response": "OK\n", "num_turns": 1}
        r = _adapt_json_envelope(env, 0, 1.0, "")
        assert "finished_reason" not in r and "partial" not in r

    def test_execute_cli_returns_partial_and_does_not_raise(self, agy_1_5_3, monkeypatch):
        # Exit 3 + AGY_ERROR retryable:false. Must come back as a result (with
        # the partial answer), not as a rate limit or an execution failure.
        stub_subprocess(
            monkeypatch,
            json.dumps(PRINT_TIMEOUT_ENVELOPE).encode(),
            PRINT_TIMEOUT_STDERR.encode(),
            rc=3,
        )
        import asyncio
        r = asyncio.run(execute_cli(["--print", "write an essay"], timeout=60))
        assert r["status"] == "error"
        assert r["error_code"] == "TIMEOUT"
        assert r["partial"] is True
        assert r["agy_error"]["reason"] == "print_timeout"
        assert r["agy_error"]["retryable"] is False


# ---------------------------------------------------------------------------
# AI-credit pin
# ---------------------------------------------------------------------------

class TestAiCreditPin:

    def test_inherit_passes_nothing(self, agy_1_5_3):
        args, _ = _apply_print_runtime_flags(["--print", "hi"], 300)
        assert "--settings" not in args

    def test_off_pins_false(self, agy_1_5_3, monkeypatch):
        monkeypatch.setattr(cu, "CLI_AI_CREDITS", "off")
        args, _ = _apply_print_runtime_flags(["--print", "hi"], 300)
        assert json.loads(_flag_value(args, "--settings")) == {"useG1Credits": False}

    def test_on_pins_true(self, agy_1_5_3, monkeypatch):
        monkeypatch.setattr(cu, "CLI_AI_CREDITS", "on")
        args, _ = _apply_print_runtime_flags(["--print", "hi"], 300)
        assert json.loads(_flag_value(args, "--settings")) == {"useG1Credits": True}

    def test_pinned_even_when_version_unknown(self, agy_1_5_3, monkeypatch):
        # A billing control must not be dropped silently: an agy without the
        # flag fails loudly instead.
        monkeypatch.setattr(cu, "CLI_AI_CREDITS", "off")
        _pin_version(monkeypatch, "")
        args, _ = _apply_print_runtime_flags(["--print", "hi"], 300)
        assert "--settings" in args

    def test_caller_settings_not_overridden(self, agy_1_5_3, monkeypatch):
        monkeypatch.setattr(cu, "CLI_AI_CREDITS", "off")
        base = ["--settings", '{"verbosity": "high"}', "--print", "hi"]
        args, _ = _apply_print_runtime_flags(list(base), 300)
        assert args.count("--settings") == 1

    def test_prompt_text_does_not_suppress_the_pin(self, agy_1_5_3, monkeypatch):
        monkeypatch.setattr(cu, "CLI_AI_CREDITS", "off")
        args, _ = _apply_print_runtime_flags(["--print", "--settings"], 300)
        assert args.count("--settings") == 2  # injected flag + the prompt text

    def test_subcommands_untouched(self, agy_1_5_3, monkeypatch):
        monkeypatch.setattr(cu, "CLI_AI_CREDITS", "off")
        args, _ = _apply_print_runtime_flags(["models"], 30)
        assert args == ["models"]


# ---------------------------------------------------------------------------
# Effort, base slugs, agents
# ---------------------------------------------------------------------------

class TestEffortXhigh:

    def test_xhigh_passed_on_1_5_3(self, agy_1_5_3):
        args = _build_cli_args(prompt="hi", model="gemini-3.8-flash", effort="xhigh")
        assert _flag_value(args, "--effort") == "xhigh"

    def test_xhigh_skipped_below_floor(self, monkeypatch):
        _pin_version(monkeypatch, "1.5.2")
        args = _build_cli_args(prompt="hi", effort="xhigh")
        assert "--effort" not in args

    @pytest.mark.asyncio
    async def test_validate_effort_warns_below_floor(self, monkeypatch):
        _pin_version(monkeypatch, "1.5.2")
        meta = await validate_effort("xhigh")
        assert "1.5.3" in meta["warning"]

    @pytest.mark.asyncio
    async def test_flash_has_no_xhigh(self, agy_1_5_3):
        # Measured: `gemini-3.8-flash has no "xhigh" effort (available: low,
        # medium, high)`.
        meta = await validate_effort("xhigh", "gemini-3.8-flash")
        assert "xhigh" in meta["warning"]

    def test_xhigh_tier_is_not_mistaken_for_high(self):
        assert _pinned_tier("gemini-x-xhigh") == "xhigh"
        assert _pinned_tier("gemini-3.8-flash-high") == "high"


class TestBaseSlugWithoutEffort:

    @pytest.mark.asyncio
    async def test_accepted_from_1_3_4(self, agy_1_5_3):
        assert await validate_model("gemini-3.8-flash") == {}

    @pytest.mark.asyncio
    async def test_still_warned_below_1_3_4(self, agy_1_5_3, monkeypatch):
        _pin_version(monkeypatch, "1.3.3")
        meta = await validate_model("gemini-3.8-flash")
        assert "base slug" in meta["warning"]

    @pytest.mark.asyncio
    async def test_removed_pro_is_flagged(self, agy_1_5_3):
        meta = await validate_model("gemini-3.1-pro-high")
        assert "not recognized" in meta["warning"]


class TestAgentValidation:

    @pytest.mark.asyncio
    async def test_agy_enforces_from_1_3_4(self, monkeypatch):
        # `agy agents` lists custom agents only, so a local check could only
        # false-warn; agy's own `unknown agent` error is authoritative.
        _pin_version(monkeypatch, "1.5.3")

        async def _boom():
            raise AssertionError("agent list must not be consulted")

        monkeypatch.setattr(cu, "get_available_agents", _boom)
        assert await validate_agent("anything") == {}

    @pytest.mark.asyncio
    async def test_checked_locally_below_1_3_4(self, monkeypatch):
        _pin_version(monkeypatch, "1.3.3")

        async def _agents():
            return ["reviewer"]

        monkeypatch.setattr(cu, "get_available_agents", _agents)
        assert "not recognized" in (await validate_agent("typo"))["warning"]


# ---------------------------------------------------------------------------
# Enforced read-only via --mode plan
# ---------------------------------------------------------------------------

class TestReadonlyPlanMode:

    def test_enforced_on_1_5_3(self, agy_1_5_3):
        assert resolve_readonly_enforcement("IMPORTANT: read-only\n\nhi") == "plan_mode"

    def test_prompt_only_below_floor(self, monkeypatch):
        _pin_version(monkeypatch, "1.5.2")
        assert resolve_readonly_enforcement("hi") == "prompt_only"

    def test_prompt_only_when_version_unknown(self, monkeypatch):
        # Plan mode would REPLACE --disable-slash-commands; on an unknown agy
        # that trade cannot be shown to be worth it.
        for v in ("", "v2"):
            _pin_version(monkeypatch, v)
            assert resolve_readonly_enforcement("hi") == "prompt_only"

    def test_leading_slash_prompt_never_gets_plan_mode(self, agy_1_5_3):
        # Plan mode leaves slash expansion on, and agy expands at message start.
        assert resolve_readonly_enforcement("  /schedule every hour") == "prompt_only"

    def test_plan_mode_args(self, agy_1_5_3):
        args = _build_cli_args(prompt="read-only preamble\n\nhi", readonly=True)
        assert _flag_value(args, "--mode") == "plan"
        assert args.count("--mode") == 1
        # Plan mode is built on /plan expansion; the flag would disable it.
        assert "--disable-slash-commands" not in args

    def test_readonly_falls_back_to_defaults_below_floor(self, monkeypatch):
        _pin_version(monkeypatch, "1.5.2")
        args = _build_cli_args(prompt="hi", readonly=True)
        assert _flag_value(args, "--mode") == "accept-edits"
        assert "--disable-slash-commands" in args

    def test_not_readonly_is_unchanged(self, agy_1_5_3):
        args = _build_cli_args(prompt="hi")
        assert _flag_value(args, "--mode") == "accept-edits"
        assert "--disable-slash-commands" in args

    @pytest.mark.asyncio
    async def test_gemini_prompt_reports_enforcement(self, agy_1_5_3, monkeypatch):
        import mcp_server
        captured = {}

        async def _fake_run(args, **kw):
            captured["args"] = args
            return {"status": "success", "stdout": "ok", "stderr": "", "return_code": 0}

        monkeypatch.setattr(mcp_server, "execute_cli_with_retry", _fake_run)
        out = json.loads(await mcp_server.gemini_prompt(prompt="/clear analyse", readonly=True))
        # The preamble moves the caller's leading slash off the start.
        assert out["readonly_enforcement"] == "plan_mode"
        assert _flag_value(captured["args"], "--mode") == "plan"

        out = json.loads(await mcp_server.gemini_prompt(prompt="hi"))
        assert "readonly_enforcement" not in out
        assert _flag_value(captured["args"], "--mode") == "accept-edits"


# ---------------------------------------------------------------------------
# Shipped defaults that agy no longer offers
# ---------------------------------------------------------------------------

class TestDropUnavailableDefault:

    @pytest.mark.asyncio
    async def test_listed_model_kept(self, agy_1_5_3):
        assert await drop_unavailable_default("gemini-3.8-flash-high") == ("gemini-3.8-flash-high", {})

    @pytest.mark.asyncio
    async def test_display_name_and_base_slug_kept(self, agy_1_5_3):
        assert (await drop_unavailable_default("Gemini 3.8 Flash (High)"))[0]
        assert (await drop_unavailable_default("gemini-3.8-flash"))[0] == "gemini-3.8-flash"

    @pytest.mark.asyncio
    async def test_vanished_model_dropped_with_warning(self, agy_1_5_3):
        model, meta = await drop_unavailable_default("gemini-3.1-pro-high")
        assert model is None
        assert "gemini-3.1-pro-high" in meta["warning"]

    @pytest.mark.asyncio
    async def test_unreadable_roster_keeps_model(self, agy_1_5_3, monkeypatch):
        async def _empty():
            return []

        monkeypatch.setattr(cu, "get_available_models", _empty)
        assert await drop_unavailable_default("gemini-3.1-pro-high") == ("gemini-3.1-pro-high", {})


class TestResolveTaskModel:

    @pytest.fixture
    def stale_default(self, agy_1_5_3, monkeypatch):
        from modules.config import cli_config
        monkeypatch.setitem(cli_config.TASK_MODEL_DEFAULTS, "eval_plan", "gemini-3.1-pro-high")
        monkeypatch.delenv("CLI_MODEL_EVAL_PLAN", raising=False)
        monkeypatch.delenv("GEMINI_MODEL_EVAL_PLAN", raising=False)

    @pytest.mark.asyncio
    async def test_stale_shipped_default_is_dropped(self, stale_default):
        model, meta = await resolve_task_model("eval_plan")
        assert model is None and "warning" in meta

    @pytest.mark.asyncio
    async def test_explicit_model_is_never_replaced(self, stale_default):
        # A model the caller named must fail loudly, not run something else.
        assert await resolve_task_model("eval_plan", "gemini-3.1-pro-high") == ("gemini-3.1-pro-high", {})

    @pytest.mark.asyncio
    async def test_env_override_is_never_replaced(self, stale_default, monkeypatch):
        monkeypatch.setenv("CLI_MODEL_EVAL_PLAN", "gemini-3.1-pro-low")
        assert await resolve_task_model("eval_plan") == ("gemini-3.1-pro-low", {})

    @pytest.mark.asyncio
    async def test_shipped_defaults_resolve_unchanged_on_1_5_3(self, agy_1_5_3, monkeypatch):
        from modules.config.cli_config import TASK_MODEL_DEFAULTS
        for task, default in TASK_MODEL_DEFAULTS.items():
            monkeypatch.delenv(f"CLI_MODEL_{task.upper()}", raising=False)
            monkeypatch.delenv(f"GEMINI_MODEL_{task.upper()}", raising=False)
            monkeypatch.setattr("modules.config.cli_config.DEFAULT_MODEL", "")
            assert await resolve_task_model(task) == (default, {}), task


class TestCollaborationDefaults:

    @pytest.mark.asyncio
    async def test_shipped_defaults_exist_in_1_5_3_roster(self):
        from modules.core.mcp_collaboration_engine import DEFAULT_MODELS, SYNTHESIS_MODEL
        listed = {r["slug"] for r in ROSTER_1_5_3}
        shipped = {m for v in DEFAULT_MODELS.values() for m in v.split(",")} | {SYNTHESIS_MODEL}
        assert shipped <= listed, shipped - listed

    @pytest.mark.asyncio
    async def test_vanished_default_dropped_and_reported(self, agy_1_5_3, monkeypatch):
        import modules.core.mcp_collaboration_engine as eng
        monkeypatch.setitem(eng.DEFAULT_MODELS, "validation", "gemini-3.1-pro-high,gemini-3.8-flash-medium")
        used = []

        async def _fake_execute_model(model, prompt):
            used.append(model)
            return {"status": "success", "content": "fine"}

        monkeypatch.setattr(eng, "_execute_model", _fake_execute_model)
        out = json.loads(await eng.execute_collaboration("validation", "some content"))
        assert out["status"] == "success"
        assert "gemini-3.1-pro-high" in out["warning"]
        # The dropped model runs as agy's default; the other one is untouched.
        assert used[:2] == ["", "gemini-3.8-flash-medium"]

    @pytest.mark.asyncio
    async def test_caller_models_are_not_filtered(self, agy_1_5_3, monkeypatch):
        import modules.core.mcp_collaboration_engine as eng
        used = []

        async def _fake_execute_model(model, prompt):
            used.append(model)
            return {"status": "success", "content": "fine"}

        monkeypatch.setattr(eng, "_execute_model", _fake_execute_model)
        out = json.loads(await eng.execute_collaboration(
            "validation", "some content", models="gemini-3.1-pro-high"))
        assert "warning" not in out
        assert used[0] == "gemini-3.1-pro-high"

    @pytest.mark.asyncio
    async def test_vanished_synthesis_model_is_reported(self, agy_1_5_3, monkeypatch):
        # The synthesis steps run on SYNTHESIS_MODEL even when the caller
        # passes models, so its warning must reach the response too.
        import modules.core.mcp_collaboration_engine as eng
        monkeypatch.setattr(eng, "SYNTHESIS_MODEL", "gemini-3.1-pro-low")
        used = []

        async def _fake_execute_model(model, prompt):
            used.append(model)
            return {"status": "success", "content": "fine"}

        monkeypatch.setattr(eng, "_execute_model", _fake_execute_model)
        out = json.loads(await eng.execute_collaboration(
            "validation", "some content", models="gemini-3.8-flash-medium"))
        assert "gemini-3.1-pro-low" in out["warning"]
        assert used[-1] is None  # synthesis ran as agy's default

    @pytest.mark.asyncio
    async def test_shared_stage_and_synthesis_default_warns_once(self, agy_1_5_3, monkeypatch):
        import modules.core.mcp_collaboration_engine as eng
        monkeypatch.setattr(eng, "SYNTHESIS_MODEL", "gemini-3.1-pro-high")
        monkeypatch.setitem(eng.DEFAULT_MODELS, "validation", "gemini-3.1-pro-high")

        async def _fake_execute_model(model, prompt):
            return {"status": "success", "content": "fine"}

        monkeypatch.setattr(eng, "_execute_model", _fake_execute_model)
        out = json.loads(await eng.execute_collaboration("validation", "some content"))
        assert out["warning"].count("gemini-3.1-pro-high") == 1

    @pytest.mark.asyncio
    async def test_unknown_mode_fails_before_model_discovery(self, monkeypatch):
        import modules.core.mcp_collaboration_engine as eng

        async def _boom(model):
            raise AssertionError("model discovery must not run for an invalid mode")

        monkeypatch.setattr(eng, "drop_unavailable_default", _boom)
        out = json.loads(await eng.execute_collaboration("nonsense", "x"))
        assert out["status"] == "error"
        assert out["valid_modes"] == ["sequential", "debate", "validation"]
