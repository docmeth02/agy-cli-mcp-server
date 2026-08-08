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

CLI_TIMEOUT = int(os.getenv("CLI_TIMEOUT", os.getenv("GEMINI_TIMEOUT", "300")))
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

# Extra seconds added to agy's internal --print-timeout on top of the Python
# supervisor timeout. Python stays the authoritative supervisor (it kills the
# subprocess at exactly `timeout`); agy's own timeout sits just above as a
# safety net so it never preempts the configured budget, yet still self-aborts
# if the supervisor's kill ever fails.
CLI_PRINT_TIMEOUT_GRACE = int(os.getenv("CLI_PRINT_TIMEOUT_GRACE", "30"))

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
# legitimately run far longer than the flat CLI_TIMEOUT (300s) allows. Heavy
# tools get a larger budget; everything else inherits CLI_TIMEOUT. Override any
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

    Resolution: explicit > CLI_TIMEOUT_{TASK} env > task default > CLI_TIMEOUT.
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

    return TASK_TIMEOUT_DEFAULTS.get(task, CLI_TIMEOUT)

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
GEMINI_GIT_DIFF_LIMIT = int(os.getenv("GEMINI_GIT_DIFF_LIMIT", "150000"))
GEMINI_CONTENT_COMPARISON_LIMIT = int(os.getenv("GEMINI_CONTENT_COMPARISON_LIMIT", "400000"))

# ============================================================================
# Model Configuration (agy 1.0.5+)
# ============================================================================
# Short names ("pro", "flash", "claude") were dropped in agy 1.1.4. From 1.1.5
# on, `agy models` emits two accepted forms per model: a stable slug
# ("gemini-3.1-pro-high") and a display name ("Gemini 3.1 Pro (High)"). Slugs
# are preferred here because they are explicitly documented as stable across
# releases, while display names track marketing labels.
# Empty string = let agy decide.

DEFAULT_MODEL = os.getenv(
    "CLI_DEFAULT_MODEL", os.getenv("GEMINI_DEFAULT_MODEL", "")
)
# Reserved for future model-retry logic in execute_cli_with_retry().
# Not wired yet — automatic model fallback during an MCP session is too
# complex/risky to enable without careful design.
FALLBACK_MODEL = os.getenv(
    "CLI_FALLBACK_MODEL", os.getenv("GEMINI_FALLBACK_MODEL", "")
)
ENABLE_FALLBACK = os.getenv(
    "CLI_ENABLE_FALLBACK", os.getenv("GEMINI_ENABLE_FALLBACK", "false")
).lower() == "true"

# Per-task default models, as stable slugs (agy >= 1.1.5). Display names such as
# "Gemini 3.1 Pro (High)" are equally accepted if overridden via env.
# Override any task via CLI_MODEL_{TASK} or GEMINI_MODEL_{TASK} env vars.
# Note: Gemini 3.1 Pro has no Medium tier — only High and Low.
TASK_MODEL_DEFAULTS: dict[str, Optional[str]] = {
    "eval_plan": "gemini-3.1-pro-high",
    "review_code": "gemini-3.1-pro-high",
    "verify_solution": "gemini-3.1-pro-high",
    "code_review": "gemini-3.1-pro-high",
    "extract_structured": "gemini-3.1-pro-high",
    "git_diff_review": "gemini-3.1-pro-high",
    "content_comparison": "gemini-3.1-pro-high",
    "prompt": None,
    "summarize": None,
    "summarize_files": None,
    "sandbox": None,
    "continue_conversation": None,
}


# Per-task default reasoning effort (agy >= 1.1.5). Empty by default: most model
# slugs already pin an effort tier (gemini-3.1-pro-high), so adding a second
# source of truth would just create conflicts. Set CLI_EFFORT_{TASK} to override
# per tool, or CLI_DEFAULT_EFFORT globally, when using a base slug.
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


def get_task_model(task: str, explicit: Optional[str] = None) -> Optional[str]:
    """
    Resolve the effective model for a tool invocation.

    Resolution: explicit > env var > task default > DEFAULT_MODEL > None.
    Returns None when no model should be passed (let agy decide).
    """
    if explicit:
        return explicit

    env_key = task.upper()
    env_model = os.getenv(
        f"CLI_MODEL_{env_key}", os.getenv(f"GEMINI_MODEL_{env_key}", "")
    )
    if env_model:
        return env_model

    return TASK_MODEL_DEFAULTS.get(task) or DEFAULT_MODEL or None

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
