"""
System and service tools coordination layer.

This module implements system-level tools like sandbox execution,
cache management, and rate limiting statistics.
"""
import json
import logging
from typing import Optional

from modules.utils.cli_utils import (
    execute_cli_with_retry,
    extract_file_refs,
    _build_cli_args,
    get_metrics,
    HELP_CACHE,
    VERSION_CACHE,
    PROMPT_CACHE,
    CLIExecutionError,
    CLITimeoutError,
    CLIRateLimitError,
)
from modules.config.cli_config import (
    GEMINI_SANDBOX_LIMIT,
)

logger = logging.getLogger(__name__)


async def execute_sandbox(
    prompt: str,
    model: Optional[str] = None,
    sandbox_image: Optional[str] = None
) -> dict:
    """
    Execute a prompt in sandbox mode.

    Args:
        prompt: The prompt to execute
        model: Model to use (ignored; kept for backward compatibility)
        sandbox_image: Optional Docker image for sandbox (ignored for agy)

    Returns:
        Execution result dictionary
    """
    if len(prompt) > GEMINI_SANDBOX_LIMIT:
        return {
            "status": "error",
            "error": f"Prompt exceeds limit of {GEMINI_SANDBOX_LIMIT:,} characters",
            "error_code": "INPUT_TOO_LARGE"
        }

    if sandbox_image:
        logger.warning(f"sandbox_image='{sandbox_image}' ignored: agy does not support custom sandbox images")

    cleaned_prompt, files = extract_file_refs(prompt)
    args = _build_cli_args(
        prompt=cleaned_prompt,
        sandbox=True,
        files=files
    )

    try:
        result = await execute_cli_with_retry(args)
        if model != "gemini-2.5-pro":
            result["model_ignored"] = True
        if sandbox_image:
            result["sandbox_image_ignored"] = True
        return result
    except (CLITimeoutError, CLIRateLimitError, CLIExecutionError) as e:
        error_code = type(e).__name__.replace("CLI", "").replace("Error", "").upper()
        return {"status": "error", "error": str(e), "error_code": error_code}


def get_cache_statistics() -> dict:
    """
    Get comprehensive cache statistics.

    Returns:
        Dictionary with cache statistics for all caches
    """
    stats = {
        "help_cache": {
            "size": len(HELP_CACHE),
            "maxsize": HELP_CACHE.maxsize,
            "ttl_seconds": HELP_CACHE.ttl,
            "items": list(HELP_CACHE.keys())
        },
        "version_cache": {
            "size": len(VERSION_CACHE),
            "maxsize": VERSION_CACHE.maxsize,
            "ttl_seconds": VERSION_CACHE.ttl,
            "items": list(VERSION_CACHE.keys())
        },
        "prompt_cache": {
            "size": len(PROMPT_CACHE),
            "maxsize": PROMPT_CACHE.maxsize,
            "ttl_seconds": PROMPT_CACHE.ttl,
            "item_count": len(PROMPT_CACHE)
        }
    }

    return stats


def get_rate_limiting_statistics() -> dict:
    """
    Get comprehensive rate limiting statistics.

    Returns:
        Dictionary with rate limiting statistics
    """
    metrics = get_metrics()

    rate_stats = {
        "rate_limit_hits": metrics.get("rate_limit_hits", 0),
        "fallback_count": metrics.get("fallback_count", 0),
        "commands_executed": metrics.get("commands_executed", 0),
        "success_rate": metrics.get("success_rate", 0),
        "note": "Per-model rate limiting removed: agy does not expose model selection"
    }

    return rate_stats


def get_server_metrics() -> dict:
    """
    Get comprehensive server metrics.

    Returns:
        Dictionary with server metrics
    """
    metrics = get_metrics()
    cache_stats = get_cache_statistics()

    return {
        "metrics": metrics,
        "cache_stats": cache_stats,
        "server_info": {
            "name": "antigravity-cli-mcp-server",
            "tools_available": 24,
        }
    }
