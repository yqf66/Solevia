import ast
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import threading
from urllib.error import HTTPError

import pytest

from planner_api import model_telemetry as telemetry
from planner_api.ai import (
    ClarificationContext,
    DeepSeekClarificationProvider,
    ProviderUnavailableError,
)
from planner_api.config import Settings
from planner_api.recommendation_ai import RecommendationProvider


@pytest.fixture
def events(monkeypatch):
    rows = []
    monkeypatch.setattr(telemetry, "_write", lambda _policy, event: rows.append(deepcopy(event)))
    return rows


def settings(**extra):
    return Settings(
        _env_file=None,
        llm_provider="deepseek",
        llm_api_key="private-api-key",
        model_telemetry_enabled=True,
        **extra,
    )


def response(content, **extra):
    return {"status": "completed", "output_text": content, **extra}


class Probe:
    def __init__(self, transport, policy=None):
        self._model_trace_policy = telemetry.trace_policy(policy or settings())
        self.transport = transport

    @telemetry.model_node("test.probe")
    def run(self, headers, payload):
        return telemetry.observed_model_call(
            self.transport, "http://private-host", headers, payload, 7
        )


def test_observation_preserves_request_objects_response_and_exception_identity(events):
    payload, headers = (
        {"model": "m", "input": "private-user-text"},
        {"Authorization": "private-key"},
    )
    result = response(
        "private-answer",
        usage={
            "input_tokens": 0,
            "output_tokens": 9,
            "output_tokens_details": {"reasoning_tokens": 4},
        },
    )

    def transport(url, passed_headers, passed_payload, timeout):
        assert url == "http://private-host" and timeout == 7
        assert passed_headers is headers and passed_payload is payload
        return result

    assert Probe(transport).run(headers, payload) is result
    assert payload == {"model": "m", "input": "private-user-text"}
    attempt = next(row for row in events if row["event"] == "attempt")
    assert attempt["usage"]["input_tokens"] == 0
    assert attempt["usage"]["reasoning_tokens"] == 4
    assert attempt["transport_kind"] == "INJECTED"
    assert attempt["status"] == "NODE_ACCEPTED"
    text = json.dumps(events)
    assert not any(
        private in text
        for private in ["private-user-text", "private-answer", "private-key", "private-host"]
    )

    error = HTTPError("http://secret", 402, "private error", {}, None)

    def fail(*_):
        raise error

    with pytest.raises(HTTPError) as caught:
        Probe(fail).run(headers, payload)
    assert caught.value is error
    assert events[-1]["failure_category"] == "QUOTA"


def test_real_provider_retry_records_invalid_schema_and_exact_same_payload_with_observer_off(
    events,
):
    calls = []
    outputs = [
        response('{"selections":42}'),
        response(
            '{"selections":[],"summary":"ok"}',
            usage={
                "input_tokens": 22,
                "output_tokens": 7,
                "input_tokens_details": {"cached_tokens": 16},
            },
        ),
    ]

    def transport(*args):
        calls.append(deepcopy(args))
        return outputs[len(calls) - 1]

    provider = RecommendationProvider(settings(), transport=transport)
    result = provider.rank([], "private preference", 1, capacity={"spare_minutes": 20})
    attempts = [row for row in events if row["event"] == "attempt"]
    assert [row["attempt"] for row in attempts] == [1, 2]
    assert attempts[0]["failure_category"] == "SCHEMA_INVALID"
    assert attempts[1]["status"] == "NODE_ACCEPTED"
    assert attempts[1]["usage"]["supplier_cached_input_tokens"] == 16
    assert events[-1]["attempts"] == 2 and events[-1]["retries"] == 1
    assert events[-1]["validation_scope"] == "PROVIDER_NODE_ONLY_NOT_BUSINESS_APPROVAL"
    on_payloads = [row[2] for row in calls]
    calls.clear()
    provider._model_trace_policy = telemetry.trace_policy(settings(model_telemetry_dir="unused"))
    provider._model_trace_policy = telemetry.TracePolicy(False, "unused", 65536, 1, "deepseek")
    assert provider.rank([], "private preference", 1, capacity={"spare_minutes": 20}) == result
    assert [row[2] for row in calls] == on_payloads


def test_output_length_usage_and_terminal_failure_are_distinct_from_transport_success(events):
    def transport(*_):
        return response(
            "part",
            status="incomplete",
            incomplete_details={"reason": "max_output_tokens"},
            usage={"input_tokens": 20, "output_tokens": 40},
        )

    # A received/truncated response is observable even if a caller accepts it.
    Probe(transport).run({}, {"input": "private"})
    attempt = next(row for row in events if row["event"] == "attempt")
    assert attempt["completion"] == "OUTPUT_TRUNCATED"
    assert attempt["failure_category"] == "OUTPUT_TRUNCATED"
    assert events[-1]["status"] == "SUCCEEDED"


def test_missing_usage_is_not_invented_as_measured_zero(events):
    Probe(lambda *_: response("ok")).run({}, {})
    row = next(row for row in events if row["event"] == "attempt")
    assert row["usage"]["input_tokens"] is None


def test_observer_failure_and_disabled_policy_do_not_break_provider(monkeypatch):
    def broken(*_):
        raise OSError("disk full")

    monkeypatch.setattr(telemetry, "_write", broken)
    output = response("ok")
    assert Probe(lambda *_: output).run({}, {}) is output
    probe = Probe(lambda *_: output)
    probe._model_trace_policy = telemetry.TracePolicy(False, "unused", 65536, 1, "deepseek")
    assert probe.run({}, {}) is output


def test_concurrent_nodes_have_separate_attempt_counters_and_shared_request_trace(events):
    barrier = threading.Barrier(3)

    def transport(*_):
        barrier.wait(timeout=5)
        return response("ok")

    def run(_):
        with telemetry.trace_scope():
            probe = Probe(transport)
            probe.run({}, {})
            probe.run({}, {})

    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(run, range(3)))
    finished = [row for row in events if row["event"] == "node_finished"]
    assert len({row["node_id"] for row in finished}) == 6
    assert len({row["trace_id"] for row in finished}) == 3
    assert all(row["attempts"] == 1 for row in finished)


def test_http_error_retries_record_original_cause_not_sanitized_wrapper(events):
    provider = DeepSeekClarificationProvider(
        settings(llm_max_retries=0),
        transport=lambda *_: (_ for _ in ()).throw(
            HTTPError("http://secret", 402, "balance", {}, None)
        ),
    )
    with pytest.raises(ProviderUnavailableError):
        provider.respond(ClarificationContext("test", {}, None, []))
    attempts = [row for row in events if row["event"] == "attempt"]
    assert len(attempts) == 1
    assert attempts[0]["failure_category"] == "QUOTA"
    assert all(
        row.get("failure_category") == "QUOTA" for row in events if row["event"] == "node_finished"
    )


def test_all_known_remote_model_call_sites_are_instrumented_search_is_separate():
    root = Path(__file__).parents[1] / "src/planner_api"
    count = 0
    for path in root.rglob("*.py"):
        if path.name == "model_telemetry.py" or path.as_posix().endswith("research/providers.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        for function in [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)]:
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            assert not any(ast.unparse(call.func) == "self.transport" for call in calls), path
            observed = [call for call in calls if ast.unparse(call.func) == "observed_model_call"]
            if observed:
                assert len(observed) == 1
                assert any(
                    isinstance(d, ast.Call) and ast.unparse(d.func) == "model_node"
                    for d in function.decorator_list
                )
                count += 1
    assert count == 20


def test_rotating_jsonl_is_bounded_and_no_raw_exception_is_persisted(tmp_path):
    policy = telemetry.trace_policy(
        settings(
            model_telemetry_dir=str(tmp_path),
            model_telemetry_max_bytes=65536,
            model_telemetry_backups=1,
        )
    )
    for _ in range(900):
        telemetry._emit(policy, event="cache", node="test.cache", status="HIT")
    files = list(tmp_path.glob("model-events.jsonl*"))
    assert len(files) == 2
    assert sum(file.stat().st_size for file in files) <= 2 * 65536
    for file in files:
        assert all(json.loads(line)["status"] == "HIT" for line in file.read_text().splitlines())


def test_threadpool_submission_preserves_request_trace(events):
    with telemetry.trace_scope():
        with ThreadPoolExecutor(max_workers=2) as pool:
            jobs = [
                telemetry.submit_model_task(pool, Probe(lambda *_: response("ok")).run, {}, {})
                for _ in range(2)
            ]
            for job in jobs:
                job.result()
    finished = [row for row in events if row["event"] == "node_finished"]
    assert len(finished) == 2 and finished[0]["trace_id"] == finished[1]["trace_id"]


def test_nested_repair_is_correlated_without_double_counting_transport(events):
    class Nested(Probe):
        @telemetry.model_node("test.outer")
        def parent(self):
            self.run({}, {})

    Nested(lambda *_: response("ok")).parent()
    finished = [row for row in events if row["event"] == "node_finished"]
    child, parent = finished
    assert child["parent_node_id"] == parent["node_id"]
    assert child["attempts"] == 1 and parent["attempts"] == 0


def test_business_guard_rejection_is_separate_and_preserves_error(monkeypatch, events):
    from fastapi import HTTPException
    import planner_api.config as config

    monkeypatch.setattr(config, "get_settings", settings)
    error = HTTPException(
        503,
        {"code": "PLAN_TEMPORAL_SEGMENT_CAPACITY_INVALID", "diagnostic": "private planning text"},
    )

    @telemetry.business_check("test.capacity")
    def guard():
        raise error

    with pytest.raises(HTTPException) as caught:
        guard()
    assert caught.value is error
    assert events[-1]["event"] == "validation"
    assert events[-1]["failure_category"] == "CAPACITY_CONFLICT"
    assert "private planning text" not in json.dumps(events)
