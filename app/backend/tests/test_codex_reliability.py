import json
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from pydantic import BaseModel

from planner_api.ai import ProviderUnavailableError
from planner_api.codex_runtime.reliability import validated_generation
from planner_api.codex_runtime.materials import material_tools
from planner_api.codex_runtime.research import CodexResearchOrchestrator
from planner_api.config import Settings
from planner_api.research.schemas import PageDocument


class Answer(BaseModel):
    value: int


def provider_for(results):
    calls = []
    def transport(url, headers, payload, timeout):
        calls.append((json.loads(json.dumps(payload)), timeout, headers))
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    return SimpleNamespace(transport=transport, endpoint='local', timeout=2, sleeper=lambda _: None), calls


def test_validation_repairs_return_only_accepted_result_and_include_feedback():
    provider, calls = provider_for(['{"value":"bad"}', '{"value":7}'])
    result = validated_generation(provider, {'instructions': 'preserve constraints'}, Answer.model_validate_json)
    assert result.value == 7 and len(calls) == 2
    assert 'value' in calls[1][0]['instructions'] and 'preserve constraints' in calls[1][0]['instructions']
    assert 0 < calls[1][1] <= calls[0][1] <= 2
    assert not calls[0][2]


def test_transient_failure_retries_but_auth_failure_stops_and_repeated_invalid_result_degrades():
    provider, calls = provider_for([URLError('temporary disconnection'), '{"value":9}'])
    assert validated_generation(provider, {}, Answer.model_validate_json).value == 9
    assert len(calls) == 2
    provider, calls = provider_for([URLError('Unauthorized'), '{"value":9}'])
    with pytest.raises(ProviderUnavailableError):
        validated_generation(provider, {}, Answer.model_validate_json)
    assert len(calls) == 1
    provider, calls = provider_for(['{}'] * 4)
    with pytest.raises(ProviderUnavailableError) as failure:
        validated_generation(provider, {}, Answer.model_validate_json)
    assert len(calls) == 2 and failure.value.code == 'CODEX_VALIDATION_EXHAUSTED'


def test_material_tools_require_goal_selected_source_revision_and_valid_range():
    assert material_tools({'source_ids': ['unscoped']}) == {}
    payload = material_tools({'goal_id': 'goal', 'source_ids': ['selected'],
        'sources': [{'source_id': 'selected', 'title': 'One'}, {'source_id': 'other', 'title': 'Private'}],
        'revisions': [{'source_id': 'selected', 'revision_id': 'v1'}, {'source_id': 'other', 'revision_id': 'v2'}]})
    handler = payload['solevia_tool_handler']
    assert handler('solevia_list_materials', {})['materials'] == [{'source_id': 'selected', 'title': 'One', 'revision_id': 'v1'}]
    for args in ({'source_id': 'other'}, {'source_id': '../secret'},
                 {'source_id': 'selected', 'offset': -1}, {'source_id': 'selected', 'limit': 12001}):
        with pytest.raises(ValueError):
            handler('solevia_read_material', args)


class NativeRuntime:
    def __init__(self, *, fake_quote=False, open_event=True):
        self.fake_quote = fake_quote
        self.open_event = open_event

    def generate(self, payload, timeout):
        schema = payload['solevia_schema']['title']
        if schema == 'NativeResearchResult':
            if self.open_event:
                payload['solevia_on_item']({'type': 'webSearch', 'action': {'type': 'search', 'queries': ['official docs']}})
                payload['solevia_on_item']({'type': 'webSearch', 'action': {'type': 'openPage', 'url': 'https://example.com/docs'}})
            return json.dumps({'should_search': True, 'sources': [{'url': 'https://example.com/docs',
                'title': 'Docs', 'quote': 'Invented quote.' if self.fake_quote else 'Real page evidence.'}], 'uncertain_points': []})
        if schema == 'EvidenceSpanChoices':
            data = json.loads(payload['input'])
            return json.dumps({'selections': [{'span_id': data['spans'][0]['span_id'], 'covers_aspects': ['Question']}]})
        if schema == 'AspectEvidenceReview':
            data = json.loads(payload['input'])
            return json.dumps({'verdicts': [{'aspect': 'Question', 'status': 'SUPPORTED',
                'evidence_ids': [data['evidence'][0]['evidence_id']], 'reason': '完整正文说明所问条件。', 'missing_conditions': []}]})
        raise AssertionError(schema)


class Reader:
    def __init__(self, fail=False):
        self.fail = fail
    def read(self, request):
        if self.fail:
            raise OSError('reader unavailable')
        return PageDocument(url=request.url, finalUrl=request.url, content='Real page evidence.\n\nAll conditions are described here.')


def research(**kwargs):
    return CodexResearchOrchestrator(Settings(_env_file=None, llm_provider='codex'), **kwargs).research('Question', enabled=True)


def test_native_research_needs_observed_tools_actual_body_quote_and_coverage_gate():
    ready = research(runtime=NativeRuntime(), reader=Reader())
    assert ready.status == 'READY' and ready.coverage.read_pages == 1
    assert ready.coverage.searched_queries == 1 and ready.stage_timings_ms['first_body'] >= 0
    assert ready.selected_evidence[0].quote in ready.passages[0].text
    for options in ({'reader': Reader(fail=True)}, {'runtime': NativeRuntime(fake_quote=True)}):
        packet = research(runtime=options.get('runtime', NativeRuntime()), reader=options.get('reader', Reader()))
        assert packet.status == 'PARTIAL' and packet.coverage.read_pages == 0
        assert not packet.selected_evidence
    assert research(runtime=NativeRuntime(open_event=False), reader=Reader()).status == 'FAILED'


def test_research_schema_repair_keeps_prior_work_and_has_no_duplicate_search():
    class RepairRuntime(NativeRuntime):
        def __init__(self):
            super().__init__()
            self.calls = 0
        def generate(self, payload, timeout):
            if payload['solevia_schema']['title'] == 'NativeResearchResult':
                self.calls += 1
                if self.calls == 1:
                    result = json.loads(super().generate(payload, timeout))
                    result.pop('should_search')
                    return json.dumps(result)
                data = json.loads(payload['input'])
                assert data['previous_result']['sources'] and 'should_search' in data['validation_feedback']
                assert data['completed_queries'] == ['official docs']
                return json.dumps({'should_search': True, **data['previous_result']})
            return super().generate(payload, timeout)
    runtime = RepairRuntime()
    packet = research(runtime=runtime, reader=Reader())
    assert packet.status == 'READY' and packet.coverage.rounds == 1 and runtime.calls == 2


def test_native_budget_exhaustion_stops_without_replaying_search():
    from planner_api.research.orchestrator import ResearchLimits
    class ExcessiveRuntime(NativeRuntime):
        calls = 0
        def generate(self, payload, timeout):
            self.calls += 1
            payload['solevia_on_item']({'type': 'webSearch', 'action': {'type': 'search', 'queries': ['a', 'b']}})
            raise AssertionError('Budget observer must stop the turn')
    runtime = ExcessiveRuntime()
    packet = research(runtime=runtime, reader=Reader(), limits=ResearchLimits(max_queries=1))
    assert packet.status == 'FAILED' and runtime.calls == 1
    assert packet.coverage.searched_queries == 2


def test_long_research_question_keeps_tail_conditions_in_evidence_review():
    question = '背景说明' * 80 + '；幼儿接触尚未核实，不得类推为可食用。'
    class InspectRuntime(NativeRuntime):
        def generate(self, payload, timeout):
            if payload['solevia_schema']['title'] in {'EvidenceSpanChoices', 'AspectEvidenceReview'}:
                assert json.loads(payload['input'])['required_aspects'] == [question]
            return super().generate(payload, timeout)
    packet = CodexResearchOrchestrator(Settings(_env_file=None, llm_provider='codex'),
        runtime=InspectRuntime(), reader=Reader()).research(question, enabled=True)
    assert packet.brief.required_aspects == [question]
    assert packet.subqueries[0].covers_aspects == [question]
