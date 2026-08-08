"""
Unit tests for the agy JSON transport (`--output-format json`, agy >= 1.1.8).

The envelope is authoritative for success/failure. agy's exit code is not: an
invalid `--model` exits 1, while an unhandled slash command exits 0 with
`status: SUCCESS`. These tests pin that the adapter trusts the envelope field.

No real `agy` installation required — the subprocess layer is stubbed.
"""
import asyncio
import json

import pytest

import modules.utils.cli_utils as cu
from modules.utils.cli_utils import (
    CLIProtocolError,
    CLIRateLimitError,
    _adapt_json_envelope,
    _apply_print_runtime_flags,
    _sanitize_tree,
    execute_cli,
    resolve_output_format,
)

SUCCESS_ENVELOPE = {
    "conversation_id": "3a4105bf-0583-408c-beeb-7a230c683785",
    "status": "SUCCESS",
    "response": "JSON_OK\n",
    "duration_seconds": 1.05,
    "num_turns": 1,
    "usage": {
        "input_tokens": 16262, "output_tokens": 7, "thinking_tokens": 0,
        "cache_read_tokens": 0, "total_tokens": 16269,
    },
}

ERROR_ENVELOPE = {
    "conversation_id": "",
    "status": "ERROR",
    "response": "",
    "error": 'invalid model selection (--model "bogus")',
    "duration_seconds": 0,
    "num_turns": 0,
    "usage": {"total_tokens": 0},
}


class FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout, self._stderr, self.returncode = stdout, stderr, returncode

    async def communicate(self):
        return (self._stdout, self._stderr)

    def kill(self):
        pass

    async def wait(self):
        return self.returncode


@pytest.fixture
def modern_agy(monkeypatch):
    monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.11")
    monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")


def stub_subprocess(monkeypatch, stdout: bytes, stderr: bytes = b"", rc: int = 0):
    async def _fake(*a, **kw):
        return FakeProc(stdout, stderr, rc)
    monkeypatch.setattr(cu.asyncio, "create_subprocess_exec", _fake)


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------

class TestResolveOutputFormat:

    def test_auto_uses_json_from_1_1_8(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
        assert resolve_output_format((1, 1, 8), True) == "json"
        assert resolve_output_format((1, 1, 11), True) == "json"

    def test_auto_falls_back_to_text_below_1_1_8(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
        assert resolve_output_format((1, 1, 7), True) == "text"
        assert resolve_output_format((1, 0, 5), True) == "text"

    def test_auto_uses_text_when_version_unresolvable(self, monkeypatch):
        # Guessing json wrong would mean misparsing every single result.
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
        assert resolve_output_format((0, 0, 0), False) == "text"

    def test_text_is_always_honoured(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "text")
        assert resolve_output_format((1, 1, 11), True) == "text"

    def test_forced_json_raises_below_1_1_8(self, monkeypatch):
        # A configuration error, not a silently ignored preference.
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "json")
        with pytest.raises(cu.CLIExecutionError, match="requires agy >="):
            resolve_output_format((1, 1, 7), True)

    def test_forced_json_raises_when_version_unknown(self, monkeypatch):
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "json")
        with pytest.raises(cu.CLIExecutionError):
            resolve_output_format((0, 0, 0), False)


class TestTransportFlagInjection:

    def test_json_injected_for_server_built_print(self, modern_agy):
        args, transport = _apply_print_runtime_flags(["--print", "hi"], 300)
        assert transport == "json"
        assert args[args.index("--output-format") + 1] == "json"

    def test_caller_chosen_format_is_never_overridden(self, modern_agy):
        # This is how gemini_cli keeps control of its own output shape.
        base = ["-p", "x", "--output-format", "stream-json"]
        args, transport = _apply_print_runtime_flags(list(base), 60)
        assert transport == "explicit"
        assert args.count("--output-format") == 1
        assert args[args.index("--output-format") + 1] == "stream-json"

    def test_subcommands_untouched(self, modern_agy):
        for sub in (["models"], ["agents"], ["help"], ["--version"]):
            args, transport = _apply_print_runtime_flags(list(sub), 30)
            assert transport == "none"
            assert args == sub

    def test_text_transport_injects_no_format(self, monkeypatch):
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.7")
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
        args, transport = _apply_print_runtime_flags(["--print", "hi"], 300)
        assert transport == "text"
        assert "--output-format" not in args


# ---------------------------------------------------------------------------
# Envelope adaptation
# ---------------------------------------------------------------------------

class TestAdaptJsonEnvelope:

    def test_success_maps_to_internal_contract(self):
        r = _adapt_json_envelope(dict(SUCCESS_ENVELOPE), 0, 1.5, "")
        # The legacy shape every tool already reads.
        assert r["status"] == "success"
        assert r["stdout"] == "JSON_OK\n"
        assert r["return_code"] == 0
        assert r["execution_time"] == 1.5
        # Fields only the envelope can supply.
        assert r["conversation_id"] == SUCCESS_ENVELOPE["conversation_id"]
        assert r["usage"]["total_tokens"] == 16269
        assert r["num_turns"] == 1
        assert r["agy_duration_seconds"] == 1.05

    def test_error_status_wins_over_exit_zero(self):
        # The whole point: agy can exit 0 while reporting ERROR.
        r = _adapt_json_envelope(dict(ERROR_ENVELOPE), 0, 0.1, "")
        assert r["status"] == "error"
        assert r["return_code"] == 0
        assert "invalid model selection" in r["error"]
        # The reason is echoed into stdout so text-oriented callers see it too.
        assert "invalid model selection" in r["stdout"]

    def test_success_status_wins_over_nonzero_exit(self):
        r = _adapt_json_envelope(dict(SUCCESS_ENVELOPE), 3, 0.1, "")
        assert r["status"] == "success"
        assert r["return_code"] == 3

    @pytest.mark.parametrize("status", ["success", "Success", "UNKNOWN", "", None])
    def test_any_non_success_status_is_an_error(self, status):
        env = dict(SUCCESS_ENVELOPE, status=status)
        assert _adapt_json_envelope(env, 0, 0.1, "")["status"] == "error"

    def test_missing_status_is_an_error(self):
        env = {k: v for k, v in SUCCESS_ENVELOPE.items() if k != "status"}
        assert _adapt_json_envelope(env, 0, 0.1, "")["status"] == "error"

    def test_structured_output_is_surfaced(self):
        env = dict(SUCCESS_ENVELOPE, structured_output={"answer": "4"})
        assert _adapt_json_envelope(env, 0, 0.1, "")["structured_output"] == {"answer": "4"}

    def test_absent_optional_fields_are_omitted(self):
        r = _adapt_json_envelope({"status": "SUCCESS", "response": "hi"}, 0, 0.1, "")
        for key in ("conversation_id", "usage", "num_turns", "structured_output"):
            assert key not in r


class TestEnvelopeSanitization:

    def test_leaf_strings_are_sanitized(self):
        tree = {"response": "key AKIAIOSFODNN7EXAMPLE here", "n": 1, "l": ["Bearer abc123"]}
        out = _sanitize_tree(tree)
        assert "AKIAIOSFODNN7EXAMPLE" not in out["response"]
        assert "abc123" not in out["l"][0]
        assert out["n"] == 1

    def test_envelope_with_escaped_quotes_survives_parsing(self):
        # Sanitizing raw envelope text could consume a JSON escape and break it;
        # parsing first and sanitizing the leaves cannot.
        raw = json.dumps({
            "status": "SUCCESS",
            "response": 'note password: "hunter2" end',
        })
        envelope = json.loads(raw)  # parses because we did not regex the raw text
        r = _adapt_json_envelope(envelope, 0, 0.1, "")
        assert r["status"] == "success"
        assert "hunter2" not in r["stdout"]

    def test_non_string_leaves_untouched(self):
        assert _sanitize_tree({"a": None, "b": True, "c": 1.5}) == {
            "a": None, "b": True, "c": 1.5
        }


# ---------------------------------------------------------------------------
# execute_cli over the JSON transport
# ---------------------------------------------------------------------------

class TestExecuteCliJsonTransport:

    def test_success_round_trip(self, modern_agy, monkeypatch):
        stub_subprocess(monkeypatch, json.dumps(SUCCESS_ENVELOPE).encode())
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "success"
        assert r["stdout"] == "JSON_OK\n"
        assert r["conversation_id"] == SUCCESS_ENVELOPE["conversation_id"]

    def test_error_envelope_with_exit_zero(self, modern_agy, monkeypatch):
        stub_subprocess(monkeypatch, json.dumps(ERROR_ENVELOPE).encode(), rc=0)
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "error"

    @pytest.mark.parametrize("payload", [
        b"not json at all",
        b"",
        b"{truncated",
        b'{"status": "SUCCESS"',
    ])
    def test_unparseable_envelope_raises_protocol_error(
        self, modern_agy, monkeypatch, payload
    ):
        # Never fall back to text: having asked for JSON, exit 0 proves nothing,
        # and re-running could spend quota or repeat file edits.
        stub_subprocess(monkeypatch, payload)
        with pytest.raises(CLIProtocolError):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    @pytest.mark.parametrize("payload", [b"null", b"[]", b"123", b"true", b'"str"'])
    def test_non_object_root_raises_protocol_error(
        self, modern_agy, monkeypatch, payload
    ):
        stub_subprocess(monkeypatch, payload)
        with pytest.raises(CLIProtocolError, match="non-object JSON root"):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    def test_protocol_error_is_not_flattened(self, modern_agy, monkeypatch):
        # Must stay distinguishable from a generic execution failure so the
        # retry layer can refuse to repeat it.
        stub_subprocess(monkeypatch, b"garbage")
        with pytest.raises(CLIProtocolError):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    def test_rate_limit_still_detected_on_json_path(self, modern_agy, monkeypatch):
        stub_subprocess(
            monkeypatch,
            json.dumps(ERROR_ENVELOPE).encode(),
            stderr=b"Error: quota exceeded for this window\n",
        )
        with pytest.raises(CLIRateLimitError):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    def test_text_transport_still_works(self, monkeypatch):
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.7")
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
        stub_subprocess(monkeypatch, b"plain text answer\n")
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "success"
        assert r["stdout"] == "plain text answer\n"
        assert "agy_status" not in r

    def test_text_transport_error_pattern_still_detected(self, monkeypatch):
        monkeypatch.setattr(cu, "_get_cached_or_sync_version", lambda: "1.1.7")
        monkeypatch.setattr(cu, "CLI_OUTPUT_FORMAT", "auto")
        stub_subprocess(monkeypatch, b"Error: something broke\n")
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "error"


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

class TestInvocationAwareRetry:

    def _count_attempts(self, monkeypatch, exc):
        calls = {"n": 0}

        async def _fake(args, timeout=None):
            calls["n"] += 1
            raise exc

        monkeypatch.setattr(cu, "execute_cli", _fake)

        # Bind the real sleep before patching — referring to asyncio.sleep from
        # inside the replacement would resolve to the patched attribute and recurse.
        real_sleep = asyncio.sleep

        async def _no_delay(_seconds):
            await real_sleep(0)

        monkeypatch.setattr(cu.asyncio, "sleep", _no_delay)
        return calls

    def test_mutating_invocation_is_not_retried(self, monkeypatch):
        # A whole-process retry re-runs the entire agent turn, which may have
        # already executed commands or edited files.
        calls = self._count_attempts(monkeypatch, CLIRateLimitError("limit"))
        with pytest.raises(CLIRateLimitError):
            asyncio.run(cu.execute_cli_with_retry(["--print", "x"], mutating=True))
        assert calls["n"] == 1

    def test_readonly_invocation_is_retried_when_no_work_was_done(self, monkeypatch):
        # work_done=False means agy refused before running anything, so a retry
        # cannot duplicate work.
        calls = self._count_attempts(
            monkeypatch, CLIRateLimitError("limit", work_done=False)
        )
        with pytest.raises(CLIRateLimitError):
            asyncio.run(
                cu.execute_cli_with_retry(
                    ["--print", "x"], mutating=False, max_attempts=3
                )
            )
        assert calls["n"] == 3

    def test_readonly_invocation_is_not_retried_when_work_was_done(self, monkeypatch):
        # A limit that arrives mid-run may leave commands executed and files
        # edited; repeating the turn would repeat them.
        calls = self._count_attempts(
            monkeypatch, CLIRateLimitError("limit", work_done=True)
        )
        with pytest.raises(CLIRateLimitError):
            asyncio.run(
                cu.execute_cli_with_retry(
                    ["--print", "x"], mutating=False, max_attempts=3
                )
            )
        assert calls["n"] == 1

    def test_work_done_defaults_to_true(self):
        # Unknowable on the text transport, so assume work happened.
        assert CLIRateLimitError("x").work_done is True

    def test_default_is_conservative(self, monkeypatch):
        calls = self._count_attempts(monkeypatch, CLIRateLimitError("limit"))
        with pytest.raises(CLIRateLimitError):
            asyncio.run(cu.execute_cli_with_retry(["--print", "x"]))
        assert calls["n"] == 1, "default must not retry — it may repeat file edits"

    def test_protocol_error_never_retried(self, monkeypatch):
        calls = self._count_attempts(monkeypatch, CLIProtocolError("bad envelope"))
        with pytest.raises(CLIProtocolError):
            asyncio.run(
                cu.execute_cli_with_retry(
                    ["--print", "x"], mutating=False, max_attempts=3
                )
            )
        assert calls["n"] == 1

    def test_timeout_never_retried(self, monkeypatch):
        calls = self._count_attempts(monkeypatch, cu.CLITimeoutError("slow"))
        with pytest.raises(cu.CLITimeoutError):
            asyncio.run(
                cu.execute_cli_with_retry(
                    ["--print", "x"], mutating=False, max_attempts=3
                )
            )
        assert calls["n"] == 1


class TestRateLimitFromEnvelope:
    """
    In JSON mode agy leaves stderr EMPTY and puts the failure reason in the
    envelope (verified against 1.1.11: an invalid --model gives exit 1, a
    676-byte envelope and 0 bytes of stderr). Scanning stderr alone made
    rate-limit detection — and the whole retry policy — unreachable on the
    default transport.
    """

    def _run(self, monkeypatch, envelope, stderr=b""):
        stub_subprocess(monkeypatch, json.dumps(envelope).encode(), stderr=stderr, rc=1)
        return asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    def test_quota_in_envelope_error_raises(self, modern_agy, monkeypatch):
        env = dict(ERROR_ENVELOPE, error="Error: quota exceeded for this window")
        with pytest.raises(CLIRateLimitError):
            self._run(monkeypatch, env)

    def test_work_done_false_when_counters_are_zero(self, modern_agy, monkeypatch):
        env = dict(ERROR_ENVELOPE, error="quota exceeded",
                   num_turns=0, usage={"total_tokens": 0})
        with pytest.raises(CLIRateLimitError) as exc:
            self._run(monkeypatch, env)
        assert exc.value.work_done is False

    def test_work_done_true_when_tokens_were_spent(self, modern_agy, monkeypatch):
        env = dict(ERROR_ENVELOPE, error="quota exceeded",
                   num_turns=3, usage={"total_tokens": 5000})
        with pytest.raises(CLIRateLimitError) as exc:
            self._run(monkeypatch, env)
        assert exc.value.work_done is True

    def test_non_quota_error_is_not_a_rate_limit(self, modern_agy, monkeypatch):
        env = dict(ERROR_ENVELOPE, error='invalid model selection (--model "x")')
        r = self._run(monkeypatch, env)
        assert r["status"] == "error"

    def test_stderr_quota_still_detected(self, modern_agy, monkeypatch):
        with pytest.raises(CLIRateLimitError):
            self._run(monkeypatch, dict(ERROR_ENVELOPE),
                      stderr=b"Error: quota exceeded\n")


class TestEnvelopeNoiseTolerance:
    """agy writes update notices and language-server messages to stdout when
    CLI_LOG_FILE is unset (the default), so a strict whole-string parse would
    make one stray byte a fatal failure for every call."""

    @pytest.mark.parametrize("raw", [
        b'A new version of agy is available!\n{"status":"SUCCESS","response":"hi"}',
        b'{"status":"SUCCESS","response":"hi"}\nUpdate available\n',
        b'  \n{"status":"SUCCESS","response":"hi"}\n\n',
    ])
    def test_envelope_recovered_from_surrounding_noise(
        self, modern_agy, monkeypatch, raw
    ):
        stub_subprocess(monkeypatch, raw)
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "success"
        assert r["stdout"] == "hi"

    def test_no_json_at_all_still_fails(self, modern_agy, monkeypatch):
        stub_subprocess(monkeypatch, b"total garbage, no braces here")
        with pytest.raises(CLIProtocolError, match="no envelope-shaped JSON"):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    def test_leading_json_object_is_not_mistaken_for_the_envelope(
        self, modern_agy, monkeypatch
    ):
        # The noise this tolerates is partly language-server chatter, and LSP
        # messages are themselves JSON objects. Accepting the first complete
        # value would hand back the diagnostic — reporting a successful run that
        # had already edited files as a failure, with the response discarded.
        raw = (
            b'{"jsonrpc":"2.0","method":"window/logMessage","params":{"type":3}}\n'
            b'{"status":"SUCCESS","response":"I edited 4 files","num_turns":6}'
        )
        stub_subprocess(monkeypatch, raw)
        r = asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        assert r["status"] == "success"
        assert r["stdout"] == "I edited 4 files"
        assert r["num_turns"] == 6

    def test_non_envelope_objects_only_still_fails(self, modern_agy, monkeypatch):
        stub_subprocess(monkeypatch, b'{"jsonrpc":"2.0"}\n{"unrelated":1}')
        with pytest.raises(CLIProtocolError, match="no envelope-shaped JSON"):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    def test_protocol_error_includes_a_stdout_excerpt(self, modern_agy, monkeypatch):
        # Without it, an outage caused by stdout noise is undiagnosable from the
        # tool response alone.
        stub_subprocess(monkeypatch, b"{ this is not valid json at all")
        with pytest.raises(CLIProtocolError, match="stdout excerpt"):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))


class TestSanitizeDictKeys:
    """The text transport redacted the whole payload as one string, so a
    credential appearing as a dict KEY was covered. Sanitizing only values would
    silently reopen that position — reachable via a schema with
    additionalProperties producing {"AIza...": "..."}."""

    def test_keys_are_sanitized(self):
        out = _sanitize_tree({"AIzaSyA1234567890abcdefghijklmnopqrstuv": "found"})
        assert "AIzaSyA1234567890abcdefghijklmnopqrstuv" not in str(out)

    def test_nested_keys_are_sanitized(self):
        out = _sanitize_tree(
            {"structured_output": [{"AKIAIOSFODNN7EXAMPLE": {"x": "y"}}]}
        )
        assert "AKIAIOSFODNN7EXAMPLE" not in str(out)

    def test_ordinary_keys_are_untouched(self):
        out = _sanitize_tree({"language": "python", "functions": 2})
        assert out == {"language": "python", "functions": 2}


class TestEnvelopeAmbiguityRefused:
    """Requiring a "status" key stops LSP chatter being mistaken for the
    envelope, but an injected object that HAS a status key and precedes the real
    one would still win — a failed run reported as successful with chosen text.
    Ambiguity is a protocol error, not something to resolve by picking."""

    def test_two_status_bearing_objects_refused(self, modern_agy, monkeypatch):
        raw = (
            b'{"status":"SUCCESS","response":"ATTACKER CONTROLLED"}\n'
            b'{"status":"ERROR","error":"the real failure"}'
        )
        stub_subprocess(monkeypatch, raw)
        with pytest.raises(CLIProtocolError, match="refusing to guess"):
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))

    def test_single_envelope_after_noise_still_recovered(self, modern_agy, monkeypatch):
        raw = b'{"jsonrpc":"2.0"}\n{"status":"SUCCESS","response":"real"}'
        stub_subprocess(monkeypatch, raw)
        assert asyncio.run(execute_cli(["--print", "hi"], timeout=10))["stdout"] == "real"


class TestWorkDoneRequiresExplicitZero:
    """A missing counter is not evidence of no work: {"usage":{"input_tokens":16262}}
    with no total_tokens would otherwise unlock retries after 16k tokens."""

    def _work_done(self, monkeypatch, envelope):
        env = dict(envelope, status="ERROR", error="quota exceeded")
        stub_subprocess(monkeypatch, json.dumps(env).encode(), rc=1)
        with pytest.raises(CLIRateLimitError) as exc:
            asyncio.run(execute_cli(["--print", "hi"], timeout=10))
        return exc.value.work_done

    @pytest.mark.parametrize("envelope,expected", [
        ({"num_turns": 0, "usage": {"total_tokens": 0}}, False),
        ({"num_turns": 0}, False),
        ({"usage": {"total_tokens": 0}}, False),
        ({"usage": {"input_tokens": 16262, "output_tokens": 40}}, True),
        ({"num_turns": 3, "usage": {"total_tokens": 0}}, True),
        ({}, True),
        ({"usage": "notadict"}, True),
        ({"num_turns": "0"}, True),
    ])
    def test_work_done(self, modern_agy, monkeypatch, envelope, expected):
        assert self._work_done(monkeypatch, envelope) is expected
