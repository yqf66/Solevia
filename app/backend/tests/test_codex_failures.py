"""Fault injection: no real account, quota or network requests are used."""
import json
from urllib.error import HTTPError

import pytest

from planner_api.ai import ProviderUnavailableError
from planner_api.codex_runtime.business import BusinessCodexRuntime, PendingTurn
from planner_api.codex_runtime.failures import classify_failure, retry_delay
from planner_api.codex_runtime.protocol import CodexUnavailable
from test_codex_business import BusinessServer


@pytest.mark.parametrize('detail, code, retryable', [
    ({'message': 'capacity', 'codexErrorInfo': 'usageLimitExceeded'}, 'CODEX_QUOTA_EXHAUSTED', False),
    ({'code': 'usage_limit_reached', 'httpStatusCode': 429}, 'CODEX_QUOTA_EXHAUSTED', False),
    ({'codexErrorInfo': 'contextWindowExceeded'}, 'CODEX_CONTEXT_TOO_LONG', False),
    ({'codexErrorInfo': 'Unauthorized'}, 'CODEX_LOGIN_REQUIRED', False),
    ({'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 403}}}, 'CODEX_ACCESS_DENIED', False),
    ({'message': 'model not available for this group', 'httpStatusCode': 404}, 'CODEX_MODEL_UNAVAILABLE', False),
    ({'message': 'server_is_overloaded', 'httpStatusCode': 503}, 'CODEX_MODEL_BUSY', True),
    ({'message': 'rate limit', 'httpStatusCode': 429}, 'CODEX_RATE_LIMITED', True),
    ({'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': 503}}}, 'CODEX_SERVICE_ERROR', True),
    ({'codexErrorInfo': 'ResponseStreamDisconnected'}, 'CODEX_NETWORK_ERROR', True),
    ({'codexErrorInfo': 'BadRequest'}, 'CODEX_INVALID_REQUEST', False),
    ({'codexErrorInfo': 'SandboxError'}, 'CODEX_ACCESS_DENIED', False),
    ({'message': 'content_filter'}, 'CODEX_SAFETY_BLOCKED', False),
    ({'status': 'interrupted'}, 'CODEX_INTERRUPTED', False),
    (BrokenPipeError('private path'), 'CODEX_DISCONNECTED', True),
    (TimeoutError('private endpoint'), 'CODEX_TIMEOUT', False),
    ({'message': 'unknown sk-secret https://private.invalid/path'}, 'CODEX_GENERATION_UNAVAILABLE', False),
])
def test_structured_failure_policy_and_safe_messages(detail, code, retryable):
    failure = classify_failure(detail)
    assert failure.code == code and failure.retryable is retryable
    error = failure.as_error()
    assert error.code == code and error.retryable is False
    assert all(secret not in str(error) + error.diagnostic for secret in ('sk-secret', 'private.invalid', 'private path', 'private endpoint'))


def test_http_429_quota_takes_precedence_and_retry_after_is_not_shortened():
    error = HTTPError('https://private.invalid', 429, 'insufficient_quota', {'Retry-After': '120'}, None)
    failure = classify_failure(error)
    assert failure.code == 'CODEX_QUOTA_EXHAUSTED'
    cooldown = classify_failure({'message': 'rate limit', 'retryAfterSeconds': 120})
    assert retry_delay(cooldown, 0, 30) is None
    assert retry_delay(cooldown, 0, 180) == 120


def make_runtime(tmp_path, monkeypatch, outcomes):
    class Server(BusinessServer):
        starts = 0
        def start(self): self.starts += 1
        def call(self, method, params, **kwargs):
            if method == 'turn/start':
                outcome = outcomes.pop(0)
                if isinstance(outcome, Exception):
                    self.calls.append((method, params))
                    raise outcome
                self.fail = outcome is not None
                self.outcome = outcome
            return super().call(method, params, **kwargs)
    runtime = BusinessCodexRuntime(tmp_path, server_factory=Server)
    runtime.start()
    event = runtime.server.callbacks['on_event']
    def on_event(message):
        if message['method'] == 'turn/completed' and runtime.server.outcome:
            message['params']['turn']['error'] = runtime.server.outcome
        event(message)
    runtime.server.callbacks['on_event'] = on_event
    waits = []
    def sleep(delay):
        assert not runtime.pending
        assert runtime.generation_status()['retries'][0]['attempt'] in (2, 3)
        waits.append(delay)
    runtime.sleeper = sleep
    monkeypatch.setattr('planner_api.codex_runtime.business.random.random', lambda: 0)
    return runtime, waits


def turn_calls(runtime):
    return sum(method == 'turn/start' for method, _ in runtime.server.calls)


def test_temporary_failure_retries_with_backoff_and_only_returns_complete_answer(tmp_path, monkeypatch):
    runtime, waits = make_runtime(tmp_path, monkeypatch, [
        {'message': 'network reset', 'codexErrorInfo': 'ResponseStreamDisconnected'},
        {'message': 'server_is_overloaded'}, None])
    try:
        answer = json.loads(runtime.generate({}, 30))
        assert answer['title'] == 'thread-3'
        assert turn_calls(runtime) == 3 and waits == [2, 4]
        assert not runtime.pending and not runtime.generation_status()['retries']
    finally:
        runtime.stop()


@pytest.mark.parametrize('outcome, expected', [
    ({'codexErrorInfo': 'UsageLimitExceeded'}, 'CODEX_QUOTA_EXHAUSTED'),
    ({'codexErrorInfo': 'Unauthorized'}, 'CODEX_LOGIN_REQUIRED'),
    ({'codexErrorInfo': 'ContextWindowExceeded'}, 'CODEX_CONTEXT_TOO_LONG'),
])
def test_terminal_failure_discards_partial_text_without_retry(tmp_path, monkeypatch, outcome, expected):
    runtime, waits = make_runtime(tmp_path, monkeypatch, [outcome])
    try:
        with pytest.raises(ProviderUnavailableError) as failure:
            runtime.generate({}, 30)
        assert failure.value.code == expected
        assert turn_calls(runtime) == 1 and not waits and not runtime.pending
    finally:
        runtime.stop()


def test_local_process_crash_reconnects_once_and_shutdown_never_relaunches(tmp_path, monkeypatch):
    runtime, waits = make_runtime(tmp_path, monkeypatch, [BrokenPipeError(), None])
    try:
        assert json.loads(runtime.generate({}, 30))['title'] == 'thread-2'
        assert runtime.server.starts == 2 and waits == [2]
        runtime.stop()
        with pytest.raises(ProviderUnavailableError):
            runtime.generate({}, 30)
        assert runtime.server.starts == 2
    finally:
        runtime.stop()


def test_events_do_not_publish_partial_or_terminate_a_codex_owned_retry(tmp_path):
    runtime = BusinessCodexRuntime(tmp_path, server_factory=BusinessServer)
    pending = PendingTurn(turn_id='current')
    runtime.pending['thread'] = pending
    runtime.on_event({'method': 'error', 'params': {'threadId': 'thread', 'turnId': 'old', 'error': {'codexErrorInfo': 'UsageLimitExceeded'}}})
    assert not pending.done.is_set()
    runtime.on_event({'method': 'error', 'params': {'threadId': 'thread', 'turnId': 'current', 'willRetry': True, 'error': {'codexErrorInfo': 'ResponseStreamDisconnected'}}})
    assert pending.stage == 'RETRYING' and not pending.done.is_set()
    runtime.on_event({'method': 'error', 'params': {'threadId': 'thread', 'turnId': 'current', 'willRetry': False, 'error': {'codexErrorInfo': 'UsageLimitExceeded'}}})
    assert pending.done.is_set() and classify_failure(pending.error).code == 'CODEX_QUOTA_EXHAUSTED'


@pytest.mark.parametrize('outcome', [{'message': 'network error'}, {'codexErrorInfo': 'UsageLimitExceeded'}, {'codexErrorInfo': 'ContextWindowExceeded'}])
def test_file_operation_is_not_automatically_replayed(tmp_path, monkeypatch, outcome):
    runtime, waits = make_runtime(tmp_path, monkeypatch, [outcome])
    event = runtime.server.callbacks['on_event']
    def request_file(message):
        event(message)
        if message['method'] == 'turn/started':
            runtime._tool_call({'id': 1, 'params': {'threadId': message['params']['threadId'],
                'tool': 'solevia_request_file_copy', 'arguments': {}}})
    runtime.server.callbacks['on_event'] = request_file
    try:
        with pytest.raises(ProviderUnavailableError) as failure:
            runtime.generate({'solevia_tool_handler': lambda *_: {'request_created': True}}, 30)
        assert failure.value.code == 'CODEX_OUTCOME_UNKNOWN'
        assert turn_calls(runtime) == 1 and not waits
    finally:
        runtime.stop()


def test_repeated_busy_failure_is_bounded_and_leaves_no_active_slot(tmp_path, monkeypatch):
    runtime, waits = make_runtime(tmp_path, monkeypatch, [{'message': 'server_is_overloaded'}] * 3)
    try:
        with pytest.raises(ProviderUnavailableError) as failure:
            runtime.generate({}, 30)
        assert failure.value.code == 'CODEX_MODEL_BUSY'
        assert turn_calls(runtime) == 3 and waits == [2, 4]
        assert not runtime.pending and not runtime.generation_status()['retries']
        assert runtime.slots.acquire(blocking=False)
        runtime.slots.release()
    finally:
        runtime.stop()


def test_late_failure_from_old_process_cannot_disconnect_a_new_connection(tmp_path, monkeypatch):
    runtime, waits = make_runtime(tmp_path, monkeypatch, [None])
    original = runtime._generate_once
    attempts = []
    def once(payload, timeout):
        if not attempts:
            attempts.append(1)
            # Another caller completed a reconnect before this old call failed.
            runtime.connection_epoch += 1
            raise CodexUnavailable('old connection closed', code='CODEX_DISCONNECTED')
        return original(payload, timeout)
    monkeypatch.setattr(runtime, '_generate_once', once)
    try:
        assert json.loads(runtime.generate({}, 30))['title'] == 'thread-1'
        assert runtime.connected and runtime.server.starts == 1 and waits == [2]
    finally:
        runtime.stop()


@pytest.mark.parametrize('hybrid', [False, True])
def test_quota_after_saved_week_switches_kernel_and_resumes_without_duplicates(client, session_factory, tmp_path, monkeypatch, hybrid):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from sqlalchemy import select
    from planner_api.config import get_settings
    from planner_api.kernel import KernelManager
    from planner_api.models import PlanTask, GenerationBatch
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    from planner_api.plan_service import PlanService
    from test_plan import _confirmed_route
    from test_kernel_selection import configure_api

    class InterruptedProvider(FakePlanProvider):
        requires_sequential_details = True
        failed = False
        def draft_details(self, *args, **kwargs):
            if self.failed:
                raise classify_failure({'codexErrorInfo': 'UsageLimitExceeded'}).as_error()
            return super().draft_details(*args, **kwargs)
        def draft_hybrid_details(self, *args, **kwargs):
            if self.failed:
                raise classify_failure({'codexErrorInfo': 'UsageLimitExceeded'}).as_error()
            return super().draft_hybrid_details(*args, **kwargs)

    goal_id = _confirmed_route(client)['goal_id']
    provider = InterruptedProvider()
    with session_factory() as session:
        service = PlanService(session, provider, FakePlanDiscussionProvider(), enable_hybrid_work_items=hybrid)
        service.draft_plan(goal_id)
        first = service.draft_next_details(goal_id)
        assert first.generated_weeks == 1 and first.total_weeks > 1
        plan_id = first.plan.id
        saved_tasks = {task.id: task.title for task in session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id))}
        provider.failed = True
        with pytest.raises(HTTPException) as failure:
            service.draft_next_details(goal_id)
        detail = failure.value.detail
        assert (detail.get('provider_code') or detail['code']) == 'CODEX_QUOTA_EXHAUSTED'
        assert '额度' in detail['message']
        assert service.get_workspace(goal_id).generated_weeks == 1
        assert not list(session.scalars(select(GenerationBatch).where(GenerationBatch.goal_id == goal_id, GenerationBatch.status.in_(['QUEUED', 'RUNNING']))))

    path = tmp_path / 'profiles.env'
    path.write_text('SOLEVIA_KERNEL=codex\nCODEX_MODEL=codex-choice\n', encoding='utf-8')
    monkeypatch.setenv('SOLEVIA_ENV_FILE', str(path))
    get_settings.cache_clear()
    configure_api(path)
    manager = KernelManager(SimpleNamespace(state=SimpleNamespace(codex_business=SimpleNamespace(connected=True))), busy_check=lambda: False, env_path=path)
    assert manager.switch('api')['mode'] == 'api'
    with session_factory() as session:
        resumed = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider(), enable_hybrid_work_items=hybrid)
        for _ in range(first.total_weeks):
            workspace = resumed.draft_next_details(goal_id)
            if workspace.generation_phase == 'COMPLETE':
                break
        assert workspace.generation_phase == 'COMPLETE' and workspace.plan.id == plan_id
        tasks = list(session.scalars(select(PlanTask).where(PlanTask.plan_version_id == plan_id)))
        assert all(next(task.title for task in tasks if task.id == key) == value for key, value in saved_tasks.items())
        assert len({(task.plan_day_id, task.order_in_day) for task in tasks}) == len(tasks)


def test_quota_in_background_analysis_is_terminal_and_keeps_visible_reply(session_factory):
    from sqlalchemy import select
    from planner_api.models import ClarificationAnalysisJob
    from planner_api.schemas import ConversationReply
    from planner_api.service import ClarificationService
    class Provider:
        def respond(self, _):
            return ConversationReply(assistant_message='已收到你的目标，我们可以继续补充现实条件。')
        def analyze(self, *_):
            raise classify_failure({'codexErrorInfo': 'UsageLimitExceeded'}).as_error()
    with session_factory() as session:
        service = ClarificationService(session, Provider())
        workspace = service.create_goal('想在阳台开始种花')
        worked, delay = service.process_pending_analysis()
        assert worked and delay is None
        job = session.scalar(select(ClarificationAnalysisJob).where(ClarificationAnalysisJob.goal_id == workspace.goal_id))
        assert job.status == 'FAILED' and job.last_error_code == 'CODEX_QUOTA_EXHAUSTED'
        assert workspace.messages[-1].content == '已收到你的目标，我们可以继续补充现实条件。'


def test_restart_releases_orphan_jobs_without_erasing_saved_checkpoints(client, session_factory):
    from sqlalchemy import select
    from planner_api.models import PlanGenerationJob, GenerationBatch
    from planner_api.plan_ai import FakePlanProvider
    from planner_api.plan_discussion_ai import FakePlanDiscussionProvider
    from planner_api.plan_generation import recover_interrupted_plan_generation
    from planner_api.plan_service import PlanService
    from test_plan import _confirmed_route
    goal_id = _confirmed_route(client)['goal_id']
    with session_factory() as session:
        service = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider(), enable_hybrid_work_items=True)
        workspace = service.draft_plan(goal_id)
        batch = session.scalar(select(GenerationBatch).where(GenerationBatch.goal_id == goal_id))
        checkpoint = dict(batch.staged_result_json or {})
        batch.status = 'RUNNING'
        job = PlanGenerationJob(goal_id=goal_id, status='RUNNING')
        session.add(job)
        session.commit()
        plan_id, batch_id, job_id = workspace.plan.id, batch.id, job.id
    recover_interrupted_plan_generation(session_factory)
    recover_interrupted_plan_generation(session_factory)
    with session_factory() as session:
        assert session.get(PlanGenerationJob, job_id).status == 'FAILED'
        batch = session.get(GenerationBatch, batch_id)
        assert batch.status == 'FAILED' and batch.staged_result_json == checkpoint
        workspace = PlanService(session, FakePlanProvider(), FakePlanDiscussionProvider(), enable_hybrid_work_items=True).get_workspace(goal_id)
        assert workspace.plan.id == plan_id and workspace.generation_job.status == 'FAILED'
        assert '重新启动' in workspace.generation_job.error_message


def test_frontend_safe_recovery_messages_match_backend_contract():
    import re
    from pathlib import Path
    from planner_api.codex_runtime.failures import _FAILURES, failure_for_code
    frontend = Path(__file__).resolve().parents[2] / 'frontend/src/ui/codexFailures.ts'
    if not frontend.is_file():
        pytest.skip('standalone backend checkout does not contain the web client')
    messages = dict(re.findall(r"(CODEX_[A-Z_]+): \['[^']*', '([^']*)'\]", frontend.read_text(encoding='utf-8')))
    assert set(messages) == set(_FAILURES)
    for code, text in messages.items():
        assert failure_for_code(code).message == text + '已保存的输入和计划进度会保留。'
