"""
Unit tests for model discovery, model validation and the rate-limit detector.

Deliberately free of any real `agy` dependency: these cover exactly the class of
upstream drift that previously went unnoticed because the whole suite was
skipped when the CLI was absent.

Background: agy 1.1.5 changed `agy models` from one column (display name) to two
TAB-separated columns (stable slug, then display name). The old parser kept whole
lines, so every model name — including the server's own defaults — failed
validation and every tool response carried a false "not recognized" warning.
"""
import pytest

from modules.utils.cli_utils import (
    _is_rate_limit_signal,
    _parse_models_output,
    model_accepted_values,
    model_selection_value,
    validate_model,
)


# ---------------------------------------------------------------------------
# `agy models` output parsing
# ---------------------------------------------------------------------------

class TestParseModelsOutput:

    def test_two_column_slug_and_display_name(self):
        out = "gemini-3.1-pro-high\tGemini 3.1 Pro (High)"
        assert _parse_models_output(out) == [
            {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"}
        ]

    def test_single_column_is_legacy_display_name(self):
        # agy < 1.1.5 emitted display names only, with no stable slug.
        assert _parse_models_output("Gemini 3.1 Pro (High)") == [
            {"slug": None, "display_name": "Gemini 3.1 Pro (High)"}
        ]

    def test_full_roster_round_trip(self):
        out = (
            "gemini-3.6-flash-high\tGemini 3.6 Flash (High)\n"
            "gemini-3.1-pro-high\tGemini 3.1 Pro (High)\n"
            "claude-sonnet-4-6\tClaude Sonnet 4.6 (Thinking)\n"
            "gpt-oss-120b-medium\tGPT-OSS 120B (Medium)\n"
        )
        records = _parse_models_output(out)
        assert len(records) == 4
        assert [r["slug"] for r in records] == [
            "gemini-3.6-flash-high",
            "gemini-3.1-pro-high",
            "claude-sonnet-4-6",
            "gpt-oss-120b-medium",
        ]

    def test_blank_lines_are_skipped(self):
        assert _parse_models_output("\n\ngemini-x\tGemini X\n\n") == [
            {"slug": "gemini-x", "display_name": "Gemini X"}
        ]

    def test_tabless_prose_dropped_when_two_column_output_seen(self):
        # Once we know the output is the modern two-column form, a tab-less line
        # is prose (a preamble or status message), not a model. Admitting it
        # would put an unusable value into gemini_models()' copyable `model`
        # field and make validate_model() accept it.
        records = _parse_models_output("Fetching available models...\ngemini-x\tGemini X")
        assert records == [{"slug": "gemini-x", "display_name": "Gemini X"}]

    def test_tabless_lines_kept_when_no_two_column_line_exists(self):
        # agy < 1.1.5 emitted display names only; those must still be usable.
        records = _parse_models_output("Gemini 3.1 Pro (High)\nGemini 3.5 Flash (Low)")
        assert [r["display_name"] for r in records] == [
            "Gemini 3.1 Pro (High)",
            "Gemini 3.5 Flash (Low)",
        ]
        assert all(r["slug"] is None for r in records)

    def test_display_name_containing_no_tab_but_parens(self):
        records = _parse_models_output("Gemini 3.5 Flash (Medium)")
        assert records[0]["display_name"] == "Gemini 3.5 Flash (Medium)"

    def test_empty_output(self):
        assert _parse_models_output("") == []
        assert _parse_models_output("   \n  ") == []

    def test_whitespace_around_columns_is_stripped(self):
        records = _parse_models_output("  gemini-x \t  Gemini X  ")
        assert records == [{"slug": "gemini-x", "display_name": "Gemini X"}]


class TestModelValueHelpers:

    def test_selection_prefers_slug(self):
        record = {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"}
        assert model_selection_value(record) == "gemini-3.1-pro-high"

    def test_selection_falls_back_to_display_name(self):
        assert model_selection_value(
            {"slug": None, "display_name": "Gemini 3.1 Pro (High)"}
        ) == "Gemini 3.1 Pro (High)"

    def test_accepted_values_includes_both_columns(self):
        record = {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"}
        assert model_accepted_values(record) == [
            "gemini-3.1-pro-high",
            "Gemini 3.1 Pro (High)",
        ]

    def test_accepted_values_drops_missing_slug(self):
        assert model_accepted_values(
            {"slug": None, "display_name": "Legacy Model"}
        ) == ["Legacy Model"]


# ---------------------------------------------------------------------------
# validate_model against the two-column roster
# ---------------------------------------------------------------------------

ROSTER = [
    {"slug": "gemini-3.6-flash-medium", "display_name": "Gemini 3.6 Flash (Medium)"},
    {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"},
]


@pytest.fixture
def modern_agy(monkeypatch):
    """Pin a version at/above the full-model-name cutover with a known roster."""
    import modules.utils.cli_utils as cu
    monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.11")

    async def _models():
        return list(ROSTER)

    monkeypatch.setattr(cu, "get_available_models", _models)


class TestValidateModelBothColumns:

    @pytest.mark.asyncio
    async def test_slug_accepted(self, modern_agy):
        assert await validate_model("gemini-3.1-pro-high") == {}

    @pytest.mark.asyncio
    async def test_display_name_accepted(self, modern_agy):
        assert await validate_model("Gemini 3.1 Pro (High)") == {}

    @pytest.mark.asyncio
    async def test_case_insensitive_on_both_columns(self, modern_agy):
        assert await validate_model("GEMINI-3.1-PRO-HIGH") == {}
        assert await validate_model("gemini 3.1 pro (high)") == {}

    @pytest.mark.asyncio
    async def test_surrounding_whitespace_tolerated(self, modern_agy):
        assert await validate_model("  gemini-3.1-pro-high  ") == {}

    @pytest.mark.asyncio
    async def test_task_defaults_all_validate(self, monkeypatch):
        # Guards the specific regression: the server's own configured defaults
        # must not trip the "not recognized" warning. Deliberately reads
        # TASK_MODEL_DEFAULTS rather than restating a literal, so changing a
        # default to something agy rejects fails here.
        import modules.utils.cli_utils as cu
        from modules.config.cli_config import TASK_MODEL_DEFAULTS

        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.11")

        async def _models():
            return [
                {"slug": "gemini-3.1-pro-high", "display_name": "Gemini 3.1 Pro (High)"},
                {"slug": "gemini-3.6-flash-medium", "display_name": "Gemini 3.6 Flash (Medium)"},
                {"slug": "claude-sonnet-4-6", "display_name": "Claude Sonnet 4.6 (Thinking)"},
            ]

        monkeypatch.setattr(cu, "get_available_models", _models)

        configured = [v for v in TASK_MODEL_DEFAULTS.values() if v]
        assert configured, "expected at least one pinned task default"
        for name in configured:
            assert await validate_model(name) == {}, f"task default {name!r} does not validate"

    @pytest.mark.asyncio
    async def test_unknown_model_warns(self, modern_agy):
        meta = await validate_model("totally-made-up")
        assert "not recognized" in meta["warning"]


class TestShortNameVersionGate:

    @pytest.mark.asyncio
    async def test_short_names_rejected_at_or_above_1_1_4(self, modern_agy):
        # agy 1.1.4 dropped them; re-verified against 1.1.11 that they hard-fail.
        for name in ("pro", "flash", "claude"):
            meta = await validate_model(name)
            assert "warning" in meta, f"{name} should not validate on modern agy"
            assert "dropped in agy 1.1.4" in meta["warning"]

    @pytest.mark.asyncio
    async def test_short_names_accepted_below_1_1_4(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.3")

        async def _boom():
            raise AssertionError("discovery should be skipped for legacy short names")

        monkeypatch.setattr(cu, "get_available_models", _boom)
        assert await validate_model("pro") == {}
        assert await validate_model("FLASH") == {}

    @pytest.mark.asyncio
    async def test_discovery_failure_marks_unverified(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.11")

        async def _models():
            return []

        monkeypatch.setattr(cu, "get_available_models", _models)
        assert await validate_model("anything") == {"model_validation": "unverified"}


# ---------------------------------------------------------------------------
# Rate-limit signal detection
# ---------------------------------------------------------------------------

class TestRateLimitSignal:

    @pytest.mark.parametrize("text", [
        "Error: rate limit exceeded, retry later",
        "RateLimit: slow down",
        "quota exceeded for this window",
        "quota exhausted",
        "You have exhausted your quota",
        "RESOURCE_EXHAUSTED",
        "resource exhausted",
        "429 Too Many Requests",
        "too many requests",
    ])
    def test_detects_exhaustion(self, text):
        assert _is_rate_limit_signal(text) is True

    @pytest.mark.parametrize("text", [
        # agy phrases exhaustion many ways; a false negative turns a transient
        # failure into a hard one with no retry, so all of these must match.
        "quotaExceeded",
        "QUOTA_EXCEEDED",
        "Error: quota_exceeded",
        "insufficient quota",
        "Quota limit reached for model gemini-3.1-pro-high",
        "You have no quota remaining for this window",
        "You have run out of your weekly quota",
        "Daily quota reached",
        "rate-limited, retry after 30s",
        "RATE_LIMIT_EXCEEDED",
        "You've reached your weekly limit for Gemini models",
        "weekly limit reached",
    ])
    def test_detects_agy_phrasing_variants(self, text):
        assert _is_rate_limit_signal(text) is True

    @pytest.mark.parametrize("text", [
        "",
        "   ",
        "Reply with exactly: PING_OK",
        "Wrote 3 files.",
    ])
    def test_ignores_unrelated_stderr(self, text):
        # Only stderr reaches this function, and agy writes quota *reporting*
        # (/usage, /quota) exclusively to stdout — verified as zero bytes on
        # stderr in both text and JSON modes — so there is no quota-reporting
        # false positive to guard against here.
        assert _is_rate_limit_signal(text) is False


# ---------------------------------------------------------------------------
# Version gating: safety flags must fail CLOSED
# ---------------------------------------------------------------------------

class TestSafetyFlagsFailClosed:
    """
    --mode accept-edits, --disable-slash-commands and --project must be injected
    even when the version probe fails. Gating them on a *successful* probe means
    a transient hiccup silently reverts to interactive-review mode, lets caller
    text be expanded as an agy command, and merges sessions meant to be isolated.
    """

    @pytest.mark.parametrize("version", [
        "",             # probe failed outright
        "garbage",      # non-empty but no version in it
        "2.0",          # two components — _parse_version yields (0,0,0)
        "v2",
        "agy (dev build)",
    ])
    def test_unresolvable_version_still_injects_safety_flags(self, version, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: version)

        args = cu._build_cli_args(prompt="/schedule something", project="p1")

        assert "--mode" in args, f"version={version!r} dropped --mode"
        assert args[args.index("--mode") + 1] == "accept-edits"
        assert "--disable-slash-commands" in args, (
            f"version={version!r} dropped --disable-slash-commands, so a caller "
            f"prompt beginning with '/' would be expanded as an agy command"
        )
        assert "--project" in args, f"version={version!r} dropped session isolation"

    def test_opt_in_still_honoured_when_version_unknown(self, monkeypatch):
        import modules.utils.cli_utils as cu
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "")
        args = cu._build_cli_args(prompt="/skills", interpret_slash_commands=True)
        assert "--disable-slash-commands" not in args


class TestPrintInvocationDetection:
    """The rate-limit scan is restricted to print runs: `agy help` writes its
    entire payload to stderr, so scanning it for every invocation would let one
    future help line naming a rate limit break gemini_help."""

    @pytest.mark.parametrize("args,expected", [
        (["--print", "hi"], True),
        (["-p", "hi"], True),
        (["--prompt", "hi"], True),
        (["--print=hi"], True),
        (["help"], False),
        (["models"], False),
        (["agents"], False),
        (["--version"], False),
    ])
    def test_detection(self, args, expected):
        from modules.utils.cli_utils import _is_print_invocation
        assert _is_print_invocation(args) is expected
