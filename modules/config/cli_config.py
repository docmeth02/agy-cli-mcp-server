"""
Main configuration interface for Antigravity CLI MCP Server.

This module consolidates all configuration from environment variables
and provides a unified interface for the rest of the application.
"""
import os
from typing import Optional

# ============================================================================
# Core Configuration (with backward-compatible env var fallbacks)
# ============================================================================

CLI_TIMEOUT = int(os.getenv("CLI_TIMEOUT", os.getenv("GEMINI_TIMEOUT", "900")))
CLI_COMMAND_PATH = os.getenv("CLI_COMMAND_PATH", os.getenv("GEMINI_COMMAND_PATH", "agy"))
CLI_LOG_LEVEL = os.getenv("CLI_LOG_LEVEL", os.getenv("GEMINI_LOG_LEVEL", "INFO")).upper()

# Transport for --print runs. agy 1.1.8 added `--output-format json`, whose
# envelope carries an authoritative status, the real conversation id, and token
# accounting — replacing the regex/exit-code guessing the text path needs.
#
#   auto (default) : JSON on agy >= 1.1.8, text below
#   json           : force JSON; a configuration error below 1.1.8
#   text           : force the legacy text path (escape hatch)
#
# Note this knob previously existed and defaulted to "json" while being read by
# nothing at all. It is now live, so the default is "auto" rather than "json" —
# "json" would be a hard error on older agy instead of degrading.
CLI_OUTPUT_FORMAT = os.getenv(
    "CLI_OUTPUT_FORMAT", os.getenv("GEMINI_OUTPUT_FORMAT", "auto")
).strip().lower()

_VALID_OUTPUT_FORMATS = frozenset({"auto", "text", "json"})
if CLI_OUTPUT_FORMAT not in _VALID_OUTPUT_FORMATS:
    raise ValueError(
        f"CLI_OUTPUT_FORMAT must be one of {sorted(_VALID_OUTPUT_FORMATS)}, "
        f"got {CLI_OUTPUT_FORMAT!r}"
    )

# Optional override for agy's own diagnostic log file (language-server startup,
# warnings, update checks). Set CLI_LOG_FILE to a real, writable path to keep
# that noise off stdout and make the error-pattern scan more robust. Disabled
# by default: agy already keeps stdout clean in --print mode, and the null
# device (os.devnull) is NOT a safe value here — agy hangs when --log-file
# points at /dev/null. Must be a regular file path if set.
CLI_LOG_FILE = os.getenv("CLI_LOG_FILE", "")

# Gap, in seconds, between agy's own --print-timeout and the Python supervisor
# timeout. Which one fires first depends on the agy version (see
# _agy_print_timeout in cli_utils):
#   agy >= 1.5.0 : agy stops BEFORE the supervisor, by a tenth of the budget
#                  (at least 3s, at most `grace`). It then ends the turn itself
#                  — exit 3, the partial response kept, and finished_reason
#                  "print_timeout" — instead of being killed with nothing to
#                  show. Python still kills at exactly `timeout` if agy fails
#                  to stop.
#   older / unknown : agy stops `grace` seconds AFTER the supervisor, purely as
#                  a safety net. Before 1.5.0 a print timeout was not
#                  reliably reported as a failure (1.5.0 introduced the exit 3),
#                  so a cut-off answer could pass as complete.
CLI_PRINT_TIMEOUT_GRACE = int(os.getenv("CLI_PRINT_TIMEOUT_GRACE", "30"))

# Whether agy may fall back to paid AI (G1) credits once the plan quota is used
# up, pinned per run through `--settings '{"useG1Credits": ...}'` (never saved).
#
#   inherit (default) : pass nothing; agy uses the global setting
#   off               : pin useG1Credits=false on every print run
#   on                : pin useG1Credits=true on every print run
#
# Measured on agy 1.5.3: the key is accepted (and agy rejects misspelled keys
# such as "useAICredits" with `unknown setting`). Whether "off" actually stops
# credit spend could NOT be measured — that needs an account that has credits
# and has used up its quota — so the default stays "inherit". A pinned value is
# passed whatever the agy version: an agy without --settings or without the key
# fails loudly, which beats a billing control that is silently not applied.
CLI_AI_CREDITS = os.getenv("CLI_AI_CREDITS", "inherit").strip().lower()

_VALID_AI_CREDITS = frozenset({"inherit", "off", "on"})
if CLI_AI_CREDITS not in _VALID_AI_CREDITS:
    raise ValueError(
        f"CLI_AI_CREDITS must be one of {sorted(_VALID_AI_CREDITS)}, "
        f"got {CLI_AI_CREDITS!r}"
    )

# ============================================================================
# Retry Configuration
# ============================================================================

RETRY_MAX_ATTEMPTS = int(os.getenv("RETRY_MAX_ATTEMPTS", "3"))
RETRY_BASE_DELAY = float(os.getenv("RETRY_BASE_DELAY", "1.0"))
RETRY_MAX_DELAY = float(os.getenv("RETRY_MAX_DELAY", "30.0"))

# ============================================================================
# Per-Task Timeout Configuration
# ============================================================================
# agy 1.0.7 raised the per-run tool-call ceiling to 512, so agentic tools can
# legitimately run for many minutes. CLI_TIMEOUT defaults to 900s: the old 300s
# default was the source of reported "5 minute timeouts" (agy itself has had no
# headless time limit since 1.2.6). A task default only ever RAISES a tool above
# CLI_TIMEOUT, never below it, so raising the global raises every tool. Override any
# task via CLI_TIMEOUT_{TASK} or GEMINI_TIMEOUT_{TASK}. Note: timeouts are NOT
# retried (see execute_cli_with_retry), so this value is the true wall-clock cap.

TASK_TIMEOUT_DEFAULTS: dict[str, int] = {
    "verify_solution": 900,
    "code_review": 900,
    "review_code": 600,
    "eval_plan": 600,
    "ai_collaboration": 900,
    "summarize_files": 600,
    "content_comparison": 600,
    "sandbox": 600,
}


def get_task_timeout(task: str, explicit: Optional[int] = None) -> int:
    """
    Resolve the effective subprocess timeout (seconds) for a tool invocation.

    Resolution: explicit > CLI_TIMEOUT_{TASK} env > max(task default, CLI_TIMEOUT).
    """
    if explicit:
        return explicit

    env_key = task.upper()
    env_timeout = os.getenv(
        f"CLI_TIMEOUT_{env_key}", os.getenv(f"GEMINI_TIMEOUT_{env_key}", "")
    )
    if env_timeout:
        try:
            return int(env_timeout)
        except ValueError:
            pass

    return max(TASK_TIMEOUT_DEFAULTS.get(task, CLI_TIMEOUT), CLI_TIMEOUT)

# ============================================================================
# Tool-Specific Character Limits
# ============================================================================
# Kept under original GEMINI_* names for backward compatibility of callers.

GEMINI_PROMPT_LIMIT = int(os.getenv("GEMINI_PROMPT_LIMIT", "100000"))
GEMINI_SANDBOX_LIMIT = int(os.getenv("GEMINI_SANDBOX_LIMIT", "200000"))
GEMINI_SUMMARIZE_LIMIT = int(os.getenv("GEMINI_SUMMARIZE_LIMIT", "400000"))
GEMINI_SUMMARIZE_FILES_LIMIT = int(os.getenv("GEMINI_SUMMARIZE_FILES_LIMIT", "800000"))
GEMINI_EVAL_LIMIT = int(os.getenv("GEMINI_EVAL_LIMIT", "500000"))
GEMINI_REVIEW_LIMIT = int(os.getenv("GEMINI_REVIEW_LIMIT", "300000"))
GEMINI_VERIFY_LIMIT = int(os.getenv("GEMINI_VERIFY_LIMIT", "800000"))
GEMINI_COLLABORATION_LIMIT = int(os.getenv("GEMINI_COLLABORATION_LIMIT", "500000"))
GEMINI_CODE_REVIEW_LIMIT = int(os.getenv("GEMINI_CODE_REVIEW_LIMIT", "300000"))
GEMINI_EXTRACT_STRUCTURED_LIMIT = int(os.getenv("GEMINI_EXTRACT_STRUCTURED_LIMIT", "200000"))
GEMINI_GIT_DIFF_LIMIT = int(os.getenv("GEMINI_GIT_DIFF_LIMIT", "300000"))
GEMINI_CONTENT_COMPARISON_LIMIT = int(os.getenv("GEMINI_CONTENT_COMPARISON_LIMIT", "400000"))

# ============================================================================
# Model Configuration (agy 1.0.5+)
# ============================================================================
# Short names ("pro", "flash", "claude") were dropped in agy 1.1.4. From 1.1.5
# on, `agy models` emits two accepted forms per model: a stable slug
# ("gemini-3.8-flash-high") and a display name ("Gemini 3.8 Flash (High)").
# Slugs are preferred here because they are explicitly documented as stable
# across releases, while display names track marketing labels.
# Empty string = let agy decide.

DEFAULT_MODEL = os.getenv(
    "CLI_DEFAULT_MODEL", os.getenv("GEMINI_DEFAULT_MODEL", "")
)
# Reserved for future model-retry logic in execute_cli_with_retry().
# Not wired yet — automatic model fallback during an MCP session is too
# complex/risky to enable without careful design. (Unrelated to
# resolve_task_model, which only leaves out a *shipped default* that
# `agy models` no longer lists, before the run starts.)
FALLBACK_MODEL = os.getenv(
    "CLI_FALLBACK_MODEL", os.getenv("GEMINI_FALLBACK_MODEL", "")
)
ENABLE_FALLBACK = os.getenv(
    "CLI_ENABLE_FALLBACK", os.getenv("GEMINI_ENABLE_FALLBACK", "false")
).lower() == "true"

# Per-task default models, as stable slugs (agy >= 1.1.5). Display names such as
# "Gemini 3.8 Flash (High)" are equally accepted if overridden via env.
# Override any task via CLI_MODEL_{TASK} or GEMINI_MODEL_{TASK} env vars.
#
# Gemini 3.1 Pro was the heavy-task default until agy 1.5.3 dropped it from the
# roster (live list: Gemini 3.8 Flash High/Medium/Low, Claude Sonnet/Opus 4.6,
# GPT-OSS 120B), after which every tool below hard-failed on its default. The
# High tier of 3.8 Flash is the strongest Gemini model left. A shipped default
# that disappears again is dropped at run time with a warning rather than
# failing the call (see resolve_task_model in cli_utils), and
# test_shipped_default_models_exist_in_live_roster pins these against agy.
TASK_MODEL_DEFAULTS: dict[str, Optional[str]] = {
    "eval_plan": "gemini-3.8-flash-high",
    "review_code": "gemini-3.8-flash-high",
    "verify_solution": "gemini-3.8-flash-high",
    "code_review": "gemini-3.8-flash-high",
    "extract_structured": "gemini-3.8-flash-high",
    "git_diff_review": "gemini-3.8-flash-high",
    "content_comparison": "gemini-3.8-flash-high",
    "prompt": None,
    "summarize": None,
    "summarize_files": None,
    "sandbox": None,
    "continue_conversation": None,
}


# Per-task default reasoning effort (agy >= 1.1.10; the flag exists from 1.1.5
# but was silently ignored in headless runs before 1.1.10). Empty by default: most model
# slugs already pin an effort tier (gemini-3.8-flash-high), and agy REJECTS a
# tiered slug combined with a different effort. Set CLI_EFFORT_{TASK} to
# override per tool, or CLI_DEFAULT_EFFORT globally, only when using a base slug
# (gemini-3.8-flash). Since agy 1.3.4 a base slug without an effort uses the
# model's default tier instead of failing.
DEFAULT_EFFORT = os.getenv("CLI_DEFAULT_EFFORT", os.getenv("GEMINI_DEFAULT_EFFORT", ""))

TASK_EFFORT_DEFAULTS: dict[str, Optional[str]] = {}


def get_task_effort(task: str, explicit: Optional[str] = None) -> Optional[str]:
    """
    Resolve the effective reasoning effort for a tool invocation.

    Resolution: explicit > CLI_EFFORT_{TASK} env > task default > CLI_DEFAULT_EFFORT.
    Returns None when no --effort should be passed (agy uses the model's own tier).
    """
    if explicit:
        return explicit

    env_key = task.upper()
    env_effort = os.getenv(
        f"CLI_EFFORT_{env_key}", os.getenv(f"GEMINI_EFFORT_{env_key}", "")
    )
    if env_effort:
        return env_effort

    return TASK_EFFORT_DEFAULTS.get(task) or DEFAULT_EFFORT or None


def get_task_model_with_source(
    task: str, explicit: Optional[str] = None
) -> tuple[Optional[str], Optional[str]]:
    """
    Resolve the effective model for a tool invocation, and where it came from.

    Resolution: explicit > env var > task default > DEFAULT_MODEL > None.
    The source is "explicit", "env", "task_default", "global_default" or None
    (no model: let agy decide). Only "task_default" is a value this server
    ships; every other source is a choice the caller or operator made.
    """
    if explicit:
        return explicit, "explicit"

    env_key = task.upper()
    env_model = os.getenv(
        f"CLI_MODEL_{env_key}", os.getenv(f"GEMINI_MODEL_{env_key}", "")
    )
    if env_model:
        return env_model, "env"

    if TASK_MODEL_DEFAULTS.get(task):
        return TASK_MODEL_DEFAULTS[task], "task_default"
    if DEFAULT_MODEL:
        return DEFAULT_MODEL, "global_default"
    return None, None


def get_task_model(task: str, explicit: Optional[str] = None) -> Optional[str]:
    """
    Resolve the effective model for a tool invocation.

    Resolution: explicit > env var > task default > DEFAULT_MODEL > None.
    Returns None when no model should be passed (let agy decide).
    """
    return get_task_model_with_source(task, explicit)[0]

# ============================================================================
# Rate Limiting Configuration
# ============================================================================

GEMINI_RATE_LIMIT_REQUESTS = int(os.getenv("GEMINI_RATE_LIMIT_REQUESTS", "100"))
GEMINI_RATE_LIMIT_WINDOW = int(os.getenv("GEMINI_RATE_LIMIT_WINDOW", "60"))

# ============================================================================
# Security Configuration
# ============================================================================

JSONRPC_MAX_REQUEST_SIZE = int(os.getenv("JSONRPC_MAX_REQUEST_SIZE", "1048576"))
JSONRPC_MAX_NESTING_DEPTH = int(os.getenv("JSONRPC_MAX_NESTING_DEPTH", "10"))
JSONRPC_STRICT_MODE = os.getenv("JSONRPC_STRICT_MODE", "true").lower() == "true"
