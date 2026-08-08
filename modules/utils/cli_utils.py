"""
Core utilities for Antigravity CLI (agy) subprocess execution.

This module provides the foundational functions for executing agy commands
with proper error handling, retry logic, output sanitization, and file
reference expansion (@filename → --add-dir).
"""
import asyncio
import glob
import json
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


def _record_security_event(event_type: str, severity: str, source: str,
                           details: Optional[dict] = None) -> None:
    """Record a security event if the monitor is available."""
    try:
        from security.security_monitor import get_security_monitor
        get_security_monitor().record_event(event_type, severity, source, details)
    except Exception:
        pass


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
MODELS_CACHE: TTLCache = TTLCache(maxsize=1, ttl=1800)  # 30 min
AGENTS_CACHE: TTLCache = TTLCache(maxsize=1, ttl=1800)  # 30 min
# Quota/credit state moves on its own schedule, and these reads are free of
# quota cost but not of subprocess/language-server startup cost, so they get
# short TTLs rather than the 30-min discovery TTL above.
USAGE_CACHE: TTLCache = TTLCache(maxsize=1, ttl=60)     # 1 min
CREDITS_CACHE: TTLCache = TTLCache(maxsize=1, ttl=300)  # 5 min

# Minimum agy versions for feature-gated CLI flags
_MIN_MODEL_VERSION = (1, 0, 5)     # --model
_MIN_PROJECT_VERSION = (1, 0, 12)  # --project / --new-project
_MIN_MODE_VERSION = (1, 1, 0)      # --mode
_MIN_AGENT_VERSION = (1, 1, 1)     # --agent
_MIN_NO_SLASH_VERSION = (1, 1, 9)  # --disable-slash-commands
_MIN_COMMAND_JSON_VERSION = (1, 1, 8)  # --output-format json (slash-command payloads)

# agy 1.1.4 dropped the short model names; from that version on only the full
# display name ("Gemini 3.1 Pro (High)") or the stable slug added in 1.1.5
# ("gemini-3.1-pro-high") is accepted.
_MIN_FULL_MODEL_NAME_VERSION = (1, 1, 4)

# Short names agy accepted prior to 1.1.4. Only valid below
# _MIN_FULL_MODEL_NAME_VERSION; validate_model() rejects them at or above it.
MODEL_SHORT_NAMES = frozenset({"pro", "flash", "claude"})

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

    Delegates to the security module's CredentialSanitizer which covers
    Google/OpenAI/Anthropic/AWS keys, bearer tokens, JWTs, private keys,
    and generic secret patterns.
    """
    if not output:
        return output
    from security.credential_sanitizer import sanitize_credentials
    return sanitize_credentials(output)


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


def _parse_version(version_str: str) -> tuple[int, ...]:
    """Parse a version string like '1.0.5' or 'agy 1.0.5' into a comparable tuple."""
    try:
        match = re.search(r'(\d+\.\d+\.\d+)', version_str.strip())
        if match:
            return tuple(int(x) for x in match.group(1).split("."))
        return (0, 0, 0)
    except (ValueError, AttributeError):
        return (0, 0, 0)


def _get_cached_or_sync_version() -> str:
    """Get version from cache, or fetch synchronously if empty."""
    cache_key = "version"
    if cache_key in VERSION_CACHE:
        return VERSION_CACHE[cache_key]
    try:
        import subprocess
        result = subprocess.run(
            [CLI_COMMAND_PATH, "--version"],
            capture_output=True, text=True, timeout=5,
        )
        version = result.stdout.strip() if result.returncode == 0 else ""
        if version:
            VERSION_CACHE[cache_key] = version
        return version
    except Exception as e:
        logger.warning(f"Failed to fetch CLI version synchronously: {e}")
        return ""


def _build_cli_args(
    prompt: str,
    sandbox: bool = False,
    debug: bool = False,
    files: Optional[list[str]] = None,
    conversation_id: Optional[str] = None,
    continue_conversation: bool = False,
    model: Optional[str] = None,
    agent: Optional[str] = None,
    project: Optional[str] = None,
    new_project: bool = False,
    interpret_slash_commands: bool = False,
) -> list[str]:
    """
    Build argument list for Antigravity CLI execution.

    interpret_slash_commands: when False (the default), pass
    --disable-slash-commands so a caller-supplied prompt is sent to the model
    verbatim. See the injection block below for why that is the safe default.
    """
    args: list[str] = []
    cached_version = _get_cached_or_sync_version()
    version = _parse_version(cached_version) if cached_version else (0, 0, 0)

    # Always skip permissions for MCP automation
    args.append("--dangerously-skip-permissions")

    # agy 1.1.0 changed the default mode to "request-review" which pauses for
    # interactive diff review before writes. Force accept-edits for headless use.
    if cached_version and version >= _MIN_MODE_VERSION:
        args.extend(["--mode", "accept-edits"])

    # agy 1.1.9 started expanding slash commands and skills in print mode, and
    # 1.1.11 made the interactive-only ones hard-fail there (`-p "/clear"` now
    # errors instead of reaching the model). This server relays arbitrary
    # caller-supplied prompt text, so a prompt that merely begins with "/" must
    # not be silently reinterpreted as a command. Default to literal text and
    # let the few tools that genuinely want command/skill expansion opt in.
    if not interpret_slash_commands:
        if cached_version and version >= _MIN_NO_SLASH_VERSION:
            args.append("--disable-slash-commands")

    # Attach files via --add-dir (replaces @filename)
    for f in (files or []):
        args.extend(["--add-dir", f])

    if sandbox:
        args.append("--sandbox")

    if debug:
        logger.warning("debug=True ignored: agy --debug produces a system report instead of answering prompts")

    if model:
        if not cached_version:
            logger.warning("CLI version could not be resolved; skipping --model flag")
        elif version < _MIN_MODEL_VERSION:
            logger.warning(
                f"--model requires agy >= {'.'.join(str(v) for v in _MIN_MODEL_VERSION)}, "
                f"found {cached_version}; skipping --model flag"
            )
        else:
            args.extend(["--model", model])

    if agent:
        if not cached_version:
            logger.warning("CLI version could not be resolved; skipping --agent flag")
        elif version < _MIN_AGENT_VERSION:
            logger.warning(
                f"--agent requires agy >= {'.'.join(str(v) for v in _MIN_AGENT_VERSION)}, "
                f"found {cached_version}; skipping --agent flag"
            )
        else:
            args.extend(["--agent", agent])

    if project:
        if cached_version and version >= _MIN_PROJECT_VERSION:
            args.extend(["--project", project])
        else:
            logger.warning("--project requires agy >= 1.0.12; skipping")
    elif new_project:
        if cached_version and version >= _MIN_PROJECT_VERSION:
            args.append("--new-project")
        else:
            logger.warning("--new-project requires agy >= 1.0.12; skipping")

    if conversation_id:
        args.extend(["--conversation", conversation_id])
    elif continue_conversation:
        args.append("--continue")

    args.extend(["--print", prompt])
    return args


_RATE_LIMIT_PATTERNS = (
    r'rate\s*limit',
    r'quota\s+(?:exceeded|exhausted)',
    r'(?:exceeded|exhausted|out\s+of|no\s+remaining)\s+(?:your\s+)?quota',
    r'resource[_\s]exhausted',
    r'too\s+many\s+requests',
    r'\b429\b',
)


def _is_rate_limit_signal(text: str) -> bool:
    """
    Whether CLI stderr indicates a transient rate-limit/quota-exhaustion state.

    Matches exhaustion phrasing only. A bare "quota" substring would also fire
    on agy's quota *reporting* output (`/usage`, `/quota`, added in 1.1.11) and
    on ordinary help text, which would misclassify a successful read as a
    retryable rate-limit failure.
    """
    if not text:
        return False
    return any(
        re.search(p, text, re.IGNORECASE) for p in _RATE_LIMIT_PATTERNS
    )


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
            _record_security_event("timeout", "low", "execute_cli",
                                   {"timeout_seconds": timeout})
            raise CLITimeoutError(
                f"Command timed out after {timeout} seconds. "
                f"Raise the budget via CLI_TIMEOUT (global) or "
                f"CLI_TIMEOUT_<TASK> (per tool); timeouts are not retried."
            )

        execution_time = time.time() - start_time
        METRICS["total_execution_time"] += execution_time

        stdout_str = sanitize_output(stdout.decode("utf-8", errors="replace"))
        stderr_str = sanitize_output(
            stderr.decode("utf-8", errors="replace") if stderr else ""
        )

        # Error detection: hybrid strategy for backward compatibility.
        # - agy < 1.1.1: always exit 0, errors only in stdout patterns.
        # - agy >= 1.1.1: server-side failures return non-zero exit + stderr.
        # Both paths are kept so the bridge works across agy versions.
        error_patterns = [
            r'^Error:\s+',
            r'^CLI error:\s+',
            r'^Warning:\s+conversation\s+"[^"]+"\s+not found',
        ]
        has_error_in_stdout = any(
            re.search(p, stdout_str, re.IGNORECASE | re.MULTILINE)
            for p in error_patterns
        )

        # Check stderr for rate limiting signals (takes priority).
        # Deliberately specific: a bare "quota" substring also matches ordinary
        # quota *reporting* (agy 1.1.11 added `/usage` and `/quota`), which would
        # turn a successful status read into a spurious CLIRateLimitError.
        if _is_rate_limit_signal(stderr_str):
            METRICS["rate_limit_hits"] += 1
            _record_security_event("rate_limit", "medium", "execute_cli",
                                   {"detail": stderr_str[:500]})
            raise CLIRateLimitError(f"Rate limit exceeded: {stderr_str}")

        if process.returncode != 0 or has_error_in_stdout:
            METRICS["commands_failed"] += 1
            # On non-zero exit (agy >= 1.1.1), prefer stderr as error content
            # since stdout may be empty and the real error is in stderr.
            error_output = stdout_str
            if process.returncode != 0 and stderr_str.strip() and not stdout_str.strip():
                error_output = stderr_str
            return {
                "status": "error",
                "return_code": process.returncode,
                "stdout": error_output,
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
        _record_security_event("cli_not_found", "high", "execute_cli",
                               {"path": CLI_COMMAND_PATH})
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

    Retries are for transient rate-limit errors only. Timeouts are NOT retried
    (a blind re-run rarely succeeds and would multiply the per-task budget), and
    non-transient execution errors are not retried.

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
            # Don't retry timeouts: a blind re-run rarely succeeds and would
            # multiply the (now per-task, possibly 900s) budget by max_attempts.
            last_error = e
            break

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


def _parse_models_output(stdout: str) -> list[dict]:
    """
    Parse `agy models` output into {slug, display_name} records.

    agy >= 1.1.5 emits two TAB-separated columns (the stable slug added in that
    release, then the display name):

        gemini-3.1-pro-high\tGemini 3.1 Pro (High)

    Older agy emitted the display name only. Both columns are accepted by
    --model, so the parser keeps each and lets callers pick. Single-column lines
    are treated as legacy display names with no slug, which also makes this
    tolerant of any future preamble line that carries no tab.
    """
    models: list[dict] = []
    for line in stdout.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        slug, tab, display = line.partition("\t")
        if tab and display.strip():
            models.append({
                "slug": slug.strip(),
                "display_name": display.strip(),
            })
        else:
            # agy < 1.1.5: display name only, no stable slug to offer.
            models.append({"slug": None, "display_name": line})
    return models


def model_selection_value(record: dict) -> str:
    """
    The value to pass to `--model` for a discovered model.

    Prefers the stable slug (agy >= 1.1.5) over the display name, which can
    change between releases.
    """
    return record.get("slug") or record.get("display_name") or ""


def model_accepted_values(record: dict) -> list[str]:
    """Every string agy will accept as `--model` for this record."""
    return [v for v in (record.get("slug"), record.get("display_name")) if v]


async def get_available_models() -> list[dict]:
    """
    Get available models from agy with caching.

    Returns a list of {"slug": str | None, "display_name": str} records. Use
    model_selection_value() to get the value to pass to --model.
    """
    cache_key = "models"

    if cache_key in MODELS_CACHE:
        METRICS["cache_hits"] += 1
        return MODELS_CACHE[cache_key]

    METRICS["cache_misses"] += 1
    try:
        result = await execute_cli(["models"], timeout=30)
        if result["status"] == "success" and result["stdout"]:
            models = _parse_models_output(result["stdout"])
            if models:
                MODELS_CACHE[cache_key] = models
            return models
    except Exception as e:
        logger.warning(f"Failed to fetch models list: {e}")

    return []


async def validate_model(model: Optional[str]) -> dict:
    """
    Validate a requested model name against what agy actually accepts.

    As of agy >= 1.1.2, print mode hard-fails (non-zero exit + stderr listing
    available models) on an unrecognized --model name. execute_cli() catches
    that via the returncode check, so this pre-validation now serves as an
    additional safety net and metadata enrichment rather than the primary
    detection mechanism.

    Either column of `agy models` is a valid --model value: the stable slug
    ("gemini-3.1-pro-high", agy >= 1.1.5) or the display name
    ("Gemini 3.1 Pro (High)"). Both are accepted here, case-insensitively.

    Returns a dict that is empty when there is nothing to report, otherwise
    carries a "warning" and/or "model_validation" key to merge into the
    tool response:
      - {}                                  model accepted / nothing to flag
      - {"warning": ...}                    --model skipped (old agy) OR unknown name
      - {"model_validation": "unverified"}  could not confirm (models list unavailable)
    """
    if not model:
        return {}

    # Mirror the version gate in _build_cli_args: on older agy, --model is not
    # passed at all — a distinct situation from an unrecognized model name.
    cached_version = _get_cached_or_sync_version()
    if not cached_version or _parse_version(cached_version) < _MIN_MODEL_VERSION:
        return {
            "warning": (
                f"--model '{model}' was not applied: agy "
                f">= {'.'.join(str(v) for v in _MIN_MODEL_VERSION)} is required "
                f"(found {cached_version or 'unknown'}). agy used its default model."
            )
        }

    requested = model.strip().lower()

    # Short names were only ever valid below 1.1.4. At or above that version
    # they are rejected by agy, so they must fall through to the failure path
    # below rather than being waved through.
    if _parse_version(cached_version) < _MIN_FULL_MODEL_NAME_VERSION:
        if requested in MODEL_SHORT_NAMES:
            return {}

    available = await get_available_models()
    if not available:
        # Discovery failed/empty — don't false-warn on a possibly-valid model.
        return {"model_validation": "unverified"}

    # Accept either the slug or the display name, case-insensitively.
    accepted = {
        value.strip().lower()
        for record in available
        for value in model_accepted_values(record)
    }
    if requested in accepted:
        return {}

    hint = ""
    if requested in MODEL_SHORT_NAMES:
        hint = (
            f" Short names like '{model}' were dropped in agy "
            f"{'.'.join(str(v) for v in _MIN_FULL_MODEL_NAME_VERSION)}; "
            f"pass a slug or display name from `agy models` instead."
        )

    return {
        "warning": (
            f"Model '{model}' was not recognized (not in `agy models`, as "
            f"either a slug or a display name).{hint} agy >= 1.1.2 will "
            f"hard-fail in print mode; older versions silently fall back to "
            f"the default model."
        )
    }


def add_model_metadata(result: dict, model_meta: dict) -> dict:
    """
    Merge model-validation metadata (from validate_model) into a result dict,
    without disturbing the standard response shape. No-op when metadata is empty.

    A model warning never clobbers an existing "warning" (e.g. a conversation
    "metadata update failed" notice) — the two are concatenated instead.
    """
    if not (isinstance(result, dict) and model_meta):
        return result
    for key, value in model_meta.items():
        if key == "warning" and result.get("warning"):
            result["warning"] = f"{result['warning']} | {value}"
        else:
            result[key] = value
    return result


async def run_readonly_slash_command(command: str, timeout: int = 60) -> dict:
    """
    Run one of agy's read-only print-mode slash commands and return its payload.

    agy 1.1.11 answers `/usage`, `/quota`, `/credits`, `/model`, `/effort` and
    `/skills` in print mode *without* starting an agent turn, spending quota, or
    leaving a conversation behind (verified: empty conversation_id and all-zero
    usage counters). That makes these safe and cheap to expose.

    On agy >= 1.1.8 the JSON envelope carries a fully structured
    ``command.data`` payload, which is preferred: it has full-precision values
    (e.g. remaining_fraction) where the text form rounds to whole percent.
    Below that, the tab-separated text records are returned instead.

    Args are built directly rather than via _build_cli_args() because these
    invocations must NOT get --disable-slash-commands (they *are* slash
    commands), and have no use for --model, --mode or --add-dir.

    Returns:
        {"status": "success", "data": {...} | None, "records": [[col, ...]], "raw": str}
        or {"status": "error", "error": str}
    """
    cached_version = _get_cached_or_sync_version()
    version = _parse_version(cached_version) if cached_version else (0, 0, 0)
    structured = bool(cached_version) and version >= _MIN_COMMAND_JSON_VERSION

    args = ["-p", command]
    if structured:
        args.extend(["--output-format", "json"])

    result = await execute_cli(args, timeout=timeout)
    if result["status"] != "success":
        return {
            "status": "error",
            "error": (result.get("stderr") or result.get("stdout") or "").strip()
                     or f"'{command}' failed with exit {result.get('return_code')}",
        }

    raw = result.get("stdout", "") or ""
    data = None
    text = raw

    if structured:
        try:
            envelope = json.loads(raw)
        except (ValueError, TypeError):
            # Don't silently re-run in text mode: fall through to parsing what
            # we got, and let the caller see the raw payload.
            logger.warning(f"'{command}' returned unparseable JSON envelope")
        else:
            if envelope.get("status") == "ERROR":
                return {
                    "status": "error",
                    "error": envelope.get("error") or f"'{command}' returned ERROR",
                }
            data = (envelope.get("command") or {}).get("data")
            text = envelope.get("response", "") or ""

    records = [
        line.split("\t")
        for line in text.strip().splitlines()
        if line.strip()
    ]

    return {"status": "success", "data": data, "records": records, "raw": text}


async def get_available_agents() -> list[str]:
    """Get available agents from agy with caching."""
    cache_key = "agents"

    if cache_key in AGENTS_CACHE:
        METRICS["cache_hits"] += 1
        return AGENTS_CACHE[cache_key]

    METRICS["cache_misses"] += 1
    try:
        result = await execute_cli(["agents"], timeout=30)
        if result["status"] == "success" and result["stdout"]:
            agents = [
                line.strip()
                for line in result["stdout"].strip().splitlines()
                if line.strip() and not line.strip().startswith("Available agents")
            ]
            AGENTS_CACHE[cache_key] = agents
            return agents
    except Exception as e:
        logger.warning(f"Failed to fetch agents list: {e}")

    return []


async def validate_agent(agent: Optional[str]) -> dict:
    """
    Validate a requested agent name against what agy knows.

    agy silently ignores an unknown --agent name (exit 0, no error), so a
    typo would pass unnoticed. Returns metadata to merge into the response.
    """
    if not agent:
        return {}

    cached_version = _get_cached_or_sync_version()
    if not cached_version or _parse_version(cached_version) < _MIN_AGENT_VERSION:
        return {
            "warning": (
                f"--agent '{agent}' was not applied: agy "
                f">= {'.'.join(str(v) for v in _MIN_AGENT_VERSION)} is required "
                f"(found {cached_version or 'unknown'}). agy used its default agent."
            )
        }

    available = await get_available_agents()
    if not available:
        return {"agent_validation": "unverified"}

    if agent.strip().lower() in {a.strip().lower() for a in available}:
        return {}

    return {
        "warning": (
            f"Agent '{agent}' was not recognized (not in `agy agents`). "
            f"agy may silently fall back to its default agent."
        )
    }


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
