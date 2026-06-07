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
CLI_OUTPUT_FORMAT = os.getenv("CLI_OUTPUT_FORMAT", os.getenv("GEMINI_OUTPUT_FORMAT", "json"))

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
# agy supports --model with short names ("pro", "flash", "claude") or full
# display names ("Gemini 3.5 Flash (Medium)"). Empty string = let agy decide.

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

# Per-task default models. "pro" for complex reasoning, None for agy's default.
# Override any task via CLI_MODEL_{TASK} or GEMINI_MODEL_{TASK} env vars.
TASK_MODEL_DEFAULTS: dict[str, Optional[str]] = {
    "eval_plan": "pro",
    "review_code": "pro",
    "verify_solution": "pro",
    "code_review": "pro",
    "extract_structured": "pro",
    "git_diff_review": "pro",
    "content_comparison": "pro",
    "prompt": None,
    "summarize": None,
    "summarize_files": None,
    "sandbox": None,
    "continue_conversation": None,
}


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
