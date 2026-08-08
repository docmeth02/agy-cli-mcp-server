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

    def test_mixed_columns_tolerated(self):
        # A line without a tab (e.g. a future preamble) must not corrupt the rest.
        records = _parse_models_output("Some Preamble\ngemini-x\tGemini X")
        assert records == [
            {"slug": None, "display_name": "Some Preamble"},
            {"slug": "gemini-x", "display_name": "Gemini X"},
        ]

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
    async def test_task_defaults_all_validate(self, modern_agy):
        # Guards the specific regression: the server's own defaults must not
        # trip the "not recognized" warning.
        assert await validate_model("gemini-3.1-pro-high") == {}

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
        # agy 1.1.11 added /usage and /quota reporting. These are successful
        # status reads and must not be misclassified as retryable failures.
        "Gemini Models\tWeekly Limit Remaining\t100%\t2026-08-15T13:19:11Z",
        "Quota is consumed proportionally to the cost of the tokens.",
        "Remaining credits\t0",
        "--quota  Show quota information",
        "",
    ])
    def test_ignores_quota_reporting(self, text):
        assert _is_rate_limit_signal(text) is False
