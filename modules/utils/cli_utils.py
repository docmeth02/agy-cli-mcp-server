"""
Core utilities for Antigravity CLI (agy) subprocess execution.

This module provides the foundational functions for executing agy commands
with proper error handling, retry logic, output sanitization, and file
reference expansion (@filename → --add-dir).
"""
import asyncio
import glob
import os
import random
import re
import shutil
import time
import logging
from pathlib import Path
from typing import Optional
from cachetools import TTLCache

logger = logging.getLogger(__name__)

from modules.config.cli_config import (
    CLI_TIMEOUT,
    CLI_COMMAND_PATH,
    CLI_LOG_FILE,
    CLI_PRINT_TIMEOUT_GRACE,
    RETRY_MAX_ATTEMPTS,
    RETRY_BASE_DELAY,
    RETRY_MAX_DELAY,
)

# Caches with TTL
HELP_CACHE: TTLCache = TTLCache(maxsize=1, ttl=1800)  # 30 min
VERSION_CACHE: TTLCache = TTLCache(maxsize=1, ttl=1800)  # 30 min

# Metrics tracking
METRICS = {
    "commands_executed": 0,
    "commands_succeeded": 0,
    "commands_failed": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "total_execution_time": 0.0,
    "rate_limit_hits": 0,
    "fallback_count": 0,
    "start_time": time.time(),
}


class CLIExecutionError(Exception):
    """Base exception for CLI execution errors."""
    pass


class CLITimeoutError(CLIExecutionError):
    """Raised when CLI command times out."""
    pass


class CLIRateLimitError(CLIExecutionError):
    """Raised when rate limits are exceeded."""
    pass


def validate_cli_setup() -> bool:
    """Validate that Antigravity CLI is properly installed and configured."""
    cli_path = shutil.which(CLI_COMMAND_PATH)
    if not cli_path:
        logger.error(f"Antigravity CLI not found at: {CLI_COMMAND_PATH}")
        return False
    logger.info(f"Antigravity CLI found at: {cli_path}")
    return True


def sanitize_output(output: str) -> str:
    """
    Sanitize output to remove potentially sensitive information.

    Args:
        output: Raw output string from CLI

    Returns:
        Sanitized output string
    """
    # Remove potential API keys
    output = re.sub(r'AIza[0-9A-Za-z_-]{35}', '[REDACTED_API_KEY]', output)
    # Remove potential secrets
    output = re.sub(r'sk-[a-zA-Z0-9]{32,}', '[REDACTED_SECRET]', output)
    # Remove potential bearer tokens
    output = re.sub(r'Bearer\s+[a-zA-Z0-9._-]+', 'Bearer [REDACTED]', output)
    return output


def extract_file_refs(prompt: str) -> tuple[str, list[str]]:
    """
    Extract @filename references from prompt and expand globs.

    Antigravity CLI uses --add-dir for file context instead of inline
    @filename syntax. This function extracts @path tokens, expands
    wildcards with glob, and returns a cleaned prompt plus resolved paths.

    Only paths that resolve within the current workspace are accepted;
    paths outside the workspace root are rejected to prevent path traversal.

    Args:
        prompt: Raw prompt string potentially containing @refs

    Returns:
        Tuple of (cleaned_prompt, list_of_resolved_paths)
    """
    pattern = r'@([^\s]+)'
    matches = re.findall(pattern, prompt)

    cleaned = re.sub(pattern, r'\1', prompt)

    workspace_root = Path(os.getcwd()).resolve()
    paths: list[str] = []
    for raw_match in set(matches):
        raw_path = raw_match.rstrip(".,;)]}\"'")
        if not raw_path:
            continue

        expanded = glob.glob(raw_path)
        if expanded:
            for p in expanded:
                path_obj = Path(p).resolve()
                if path_obj.exists() and _is_within_workspace(path_obj, workspace_root):
                    paths.append(str(path_obj))
        else:
            path_obj = Path(raw_path).resolve()
            if path_obj.exists() and _is_within_workspace(path_obj, workspace_root):
                paths.append(str(path_obj))

    seen = set()
    unique_paths: list[str] = []
    for p in paths:
        if p not in seen:
            seen.add(p)
            unique_paths.append(p)

    return cleaned, unique_paths


def _is_within_workspace(path: Path, workspace_root: Path) -> bool:
    """Check that a resolved path is within the workspace root."""
    try:
        path.relative_to(workspace_root)
        return True
    except ValueError:
        logger.warning(f"Blocked path outside workspace: {path}")
        return False


def _build_cli_args(
    prompt: str,
    sandbox: bool = False,
    debug: bool = False,
    files: Optional[list[str]] = None,
    conversation_id: Optional[str] = None,
    continue_conversation: bool = False,
) -> list[str]:
    """Build argument list for Antigravity CLI execution."""
    args: list[str] = []

    # Always skip permissions for MCP automation
    args.append("--dangerously-skip-permissions")

    # Attach files via --add-dir (replaces @filename)
    for f in (files or []):
        args.extend(["--add-dir", f])

    if sandbox:
        args.append("--sandbox")

    if debug:
        # agy --debug is hidden/undocumented and produces a system report.
        # We ignore it for normal execution and log a warning.
        logger.warning("debug=True ignored: agy --debug produces a system report instead of answering prompts")

    if conversation_id:
        args.extend(["--conversation", conversation_id])
    elif continue_conversation:
        args.append("--continue")

    args.extend(["--print", prompt])
    return args


def _apply_print_runtime_flags(args: list[str], timeout: int) -> list[str]:
    """
    Inject non-interactive print-mode runtime flags onto every `--print` call,
    independent of which tool built the base args.

    - ``--print-timeout``: agy's internal print-mode timeout defaults to 5m. If
      CLI_TIMEOUT is raised above that, agy would preempt long runs before the
      Python supervisor fires. We set it to ``timeout + grace`` so agy never
      aborts before the configured budget, while the Python-side ``wait_for``
      (at exactly ``timeout``) remains the authoritative supervisor and still
      raises CLITimeoutError on overrun.
    - ``--log-file``: only when CLI_LOG_FILE is configured (opt-in), routes
      agy's own diagnostics (language-server startup, warnings, update checks)
      to that file so stdout stays a clean response payload for the error scan.

    Flags already present in ``args`` are never overridden. Non-print
    invocations (``--version``, ``help``) are returned unchanged. The prompt
    payload itself is excluded from flag detection, so a prompt whose text
    happens to start with ``--print-timeout=`` / ``--log-file=`` does not
    suppress injection.
    """
    print_flags = ("--print", "-p", "--prompt")

    # Locate the print flag and the split-form prompt payload that follows it
    # (`--print <prompt>`). The joined form (`--print=...`) has no separate
    # payload token. Anything that isn't a print invocation is left untouched.
    is_print = False
    payload_idxs: set[int] = set()
    for i, a in enumerate(args):
        if a in print_flags:
            is_print = True
            payload_idxs.add(i + 1)  # next token is the prompt (split form)
        elif any(a.startswith(f + "=") for f in print_flags):
            is_print = True  # joined form: prompt is part of this token
    if not is_print:
        return args

    # Scan flags only, never any prompt payload, in split and joined forms.
    flag_tokens = [a for j, a in enumerate(args) if j not in payload_idxs]

    def _has(flag: str) -> bool:
        return any(a == flag or a.startswith(flag + "=") for a in flag_tokens)

    injected: list[str] = []
    if not _has("--print-timeout"):
        injected += ["--print-timeout", f"{timeout + CLI_PRINT_TIMEOUT_GRACE}s"]
    if CLI_LOG_FILE and not _has("--log-file"):
        injected += ["--log-file", CLI_LOG_FILE]

    # Prepend so injected flags precede the trailing `--print <prompt>` payload.
    return injected + args


async def execute_cli(
    args: list[str],
    timeout: Optional[int] = None,
    capture_stderr: bool = True
) -> dict:
    """
    Execute Antigravity CLI command asynchronously.

    Args:
        args: Command line arguments for agy
        timeout: Optional timeout in seconds (defaults to CLI_TIMEOUT)
        capture_stderr: Whether to capture stderr output

    Returns:
        Dictionary with status, stdout, stderr, and return_code

    Raises:
        CLITimeoutError: If command times out
        CLIExecutionError: If command fails to execute
    """
    timeout = timeout or CLI_TIMEOUT
    args = _apply_print_runtime_flags(args, timeout)
    start_time = time.time()

    METRICS["commands_executed"] += 1

    try:
        logger.debug(f"Executing: {CLI_COMMAND_PATH} {' '.join(args)}")

        env = os.environ.copy()
        # Isolate agy from any IDE language server sharing this environment.
        env.pop("ANTIGRAVITY_LS_ADDRESS", None)
        # Force-suppress the account/credits header (set unconditionally, even
        # if the launch env disables it) so it can never leak into the stdout
        # the error-pattern scan parses.
        env["AGY_CLI_HIDE_ACCOUNT_INFO"] = "1"

        process = await asyncio.create_subprocess_exec(
            CLI_COMMAND_PATH,
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE if capture_stderr else None,
            env=env,
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            METRICS["commands_failed"] += 1
            raise CLITimeoutError(
                f"Command timed out after {timeout} seconds"
            )

        execution_time = time.time() - start_time
        METRICS["total_execution_time"] += execution_time

        stdout_str = sanitize_output(stdout.decode("utf-8", errors="replace"))
        stderr_str = sanitize_output(
            stderr.decode("utf-8", errors="replace") if stderr else ""
        )

        # agy always returns returncode 0, even on errors.
        # Detect errors by scanning stdout for known error patterns.
        error_patterns = [
            r'^Error:\s+',
            r'^CLI error:\s+',
            r'^Warning:\s+conversation\s+"[^"]+"\s+not found',
        ]
        has_error_in_stdout = any(
            re.search(p, stdout_str, re.IGNORECASE | re.MULTILINE)
            for p in error_patterns
        )

        # Also check stderr for rate limiting signals
        if "rate limit" in stderr_str.lower() or "quota" in stderr_str.lower():
            METRICS["rate_limit_hits"] += 1
            raise CLIRateLimitError(f"Rate limit exceeded: {stderr_str}")

        if process.returncode != 0 or has_error_in_stdout:
            METRICS["commands_failed"] += 1
            return {
                "status": "error",
                "return_code": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "execution_time": execution_time
            }
        else:
            METRICS["commands_succeeded"] += 1
            return {
                "status": "success",
                "return_code": process.returncode,
                "stdout": stdout_str,
                "stderr": stderr_str,
                "execution_time": execution_time
            }

    except (CLITimeoutError, CLIRateLimitError):
        raise
    except FileNotFoundError:
        METRICS["commands_failed"] += 1
        raise CLIExecutionError(
            f"Antigravity CLI not found at: {CLI_COMMAND_PATH}. "
            "Please ensure agy is installed and in PATH."
        )
    except Exception as e:
        METRICS["commands_failed"] += 1
        logger.error(f"Unexpected error executing CLI: {e}")
        raise CLIExecutionError(f"Execution failed: {str(e)}")


async def execute_cli_with_retry(
    args: list[str],
    timeout: Optional[int] = None,
    max_attempts: Optional[int] = None,
) -> dict:
    """
    Execute Antigravity CLI with exponential backoff retry.

    Note: Model fallback is not supported because agy does not expose
    a --model flag. Retries are for transient errors only.

    Args:
        args: Command line arguments for agy
        timeout: Optional timeout in seconds
        max_attempts: Maximum retry attempts (defaults to RETRY_MAX_ATTEMPTS)

    Returns:
        Dictionary with execution results
    """
    max_attempts = max_attempts or RETRY_MAX_ATTEMPTS
    last_error = None

    for attempt in range(1, max_attempts + 1):
        try:
            return await execute_cli(args, timeout)

        except CLIRateLimitError as e:
            last_error = e
            if attempt < max_attempts:
                delay = min(
                    RETRY_BASE_DELAY * (2 ** (attempt - 1)),
                    RETRY_MAX_DELAY
                )
                delay += random.uniform(0, delay * 0.1)
                logger.warning(
                    f"Rate limit hit, attempt {attempt}/{max_attempts}. "
                    f"Retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)

        except CLITimeoutError as e:
            last_error = e
            if attempt < max_attempts:
                logger.warning(
                    f"Timeout on attempt {attempt}/{max_attempts}, retrying..."
                )

        except CLIExecutionError as e:
            last_error = e
            # Don't retry for non-transient errors
            break

    raise last_error or CLIExecutionError("All retry attempts failed")


async def get_cli_help() -> str:
    """Get Antigravity CLI help with caching."""
    cache_key = "help"

    if cache_key in HELP_CACHE:
        METRICS["cache_hits"] += 1
        return HELP_CACHE[cache_key]

    METRICS["cache_misses"] += 1
    result = await execute_cli(["help"], timeout=30)

    output = result["stdout"] or result["stderr"]
    HELP_CACHE[cache_key] = output
    return output


async def get_cli_version() -> str:
    """Get Antigravity CLI version with caching."""
    cache_key = "version"

    if cache_key in VERSION_CACHE:
        METRICS["cache_hits"] += 1
        return VERSION_CACHE[cache_key]

    METRICS["cache_misses"] += 1
    result = await execute_cli(["--version"], timeout=30)

    output = result["stdout"] if result["status"] == "success" else result["stderr"]
    VERSION_CACHE[cache_key] = output
    return output


def get_metrics() -> dict:
    """Get current metrics."""
    uptime = time.time() - METRICS["start_time"]
    total_commands = METRICS["commands_executed"]

    return {
        **METRICS,
        "uptime_seconds": uptime,
        "success_rate": (
            METRICS["commands_succeeded"] / total_commands * 100
            if total_commands > 0 else 0
        ),
        "average_execution_time": (
            METRICS["total_execution_time"] / total_commands
            if total_commands > 0 else 0
        ),
        "cache_hit_rate": (
            METRICS["cache_hits"] /
            (METRICS["cache_hits"] + METRICS["cache_misses"]) * 100
            if (METRICS["cache_hits"] + METRICS["cache_misses"]) > 0 else 0
        ),
    }
