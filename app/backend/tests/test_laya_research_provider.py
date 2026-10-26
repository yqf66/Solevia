import pytest

from planner_api.config import Settings
from planner_api.research.laya_provider import CompactLayaResearchProvider,CompactPlan,CompactQuestion,soft_source_score
from planner_api.research.schemas import ResearchDecision,ResearchSubquery


class Analyzer:
    def __init__(self, plan):
        self.plan,self.fallbacks=plan,0

    def _request(self,*_args,**_kwargs):
        return self.plan

    def analyze(self,*_args,**_kwargs):
        self.fallbacks+=1
        return ResearchDecision(should_search=True,reason='primary')


def prepare(monkeypatch,plan):
    import planner_api.research.laya_provider as module
    class Response:
        def __enter__(self): return self
        def __exit__(self,*_): pass
        def read(self,*_): return b'{"mode":"offline-search-router","ready":true}'
    class Opener:
        def open(self,*_,**__): return Response()
    monkeypatch.setattr(module,'build_opener',lambda *_:Opener())
    analyzer=Analyzer(plan)
    return module,analyzer,CompactLayaResearchProvider(analyzer,Settings(_env_file=None))


def plan(**updates):
    return CompactPlan(should_search=True,search_prohibited=False,requires_source_scope=False,
        reason='needs evidence',objective='compatibility',required_aspects=['compatibility'],
        questions=[CompactQuestion(key='q1',query='uv Windows compatibility',intent='核查uv Windows兼容性',aspect='compatibility')]).model_copy(update=updates)


def test_gate_miss_never_removes_required_retrieval(monkeypatch):
    module,analyzer,provider=prepare(monkeypatch,plan())
    monkeypatch.setattr(module,'call_router',lambda *_:{'needs_search':False,'source_strategy':None})
    result=provider.analyze('核查兼容性')
    assert result.should_search and len(result.planned_questions)==1
    assert result.planned_questions[0].source_strategy_hint==''
    assert analyzer.fallbacks==0


def test_source_scope_is_preserved_by_full_planner(monkeypatch):
    _,analyzer,provider=prepare(monkeypatch,plan(requires_source_scope=True))
    assert provider.analyze('只用官方资料').reason=='primary'
    assert analyzer.fallbacks==1


def test_router_unavailable_falls_back(monkeypatch):
    module,analyzer,provider=prepare(monkeypatch,plan())
    def unavailable(*_): raise TimeoutError()
    monkeypatch.setattr(module,'call_router',unavailable)
    assert provider.analyze('核查兼容性').reason=='primary'
    assert analyzer.fallbacks==1


def test_preference_cannot_generate_site_scope(monkeypatch):
    module,analyzer,provider=prepare(monkeypatch,plan())
    monkeypatch.setattr(module,'call_router',lambda *_:{'needs_search':True,'source_strategy':'official'})
    result=provider.analyze('核查兼容性')
    query=result.planned_questions[0]
    assert result.should_search and query.source_scope_mode=='SOFT'
    assert query.source_strategy_hint=='official' and query.source_profiles==[]
    assert analyzer.fallbacks==0
    copied=ResearchSubquery.model_validate(query.model_dump())
    assert copied.source_scope_mode=='SOFT'
    # Only the primary planner receives source restrictions. The compact local
    # router remains a preference signal and cannot generate a hard scope.
    assert 'source_scope_mode' not in CompactQuestion.model_json_schema()['properties']
    assert soft_source_score('DOCUMENTATION','official')==4
    assert soft_source_score('COMMUNITY','official')==0


def test_router_url_stays_loopback():
    with pytest.raises(ValueError):
        Settings(_env_file=None,research_laya_base_url='https://example.com')


def test_evidence_requirement_wins_over_source_classifier(monkeypatch):
    q=CompactQuestion(key='q1',query='Python API rule',intent='核查接口规则',aspect='compatibility',purpose='DOCUMENTATION')
    module,analyzer,provider=prepare(monkeypatch,plan(questions=[q]))
    monkeypatch.setattr(module,'call_router',lambda *_:{'needs_search':True,'source_strategy':'community'})
    result=provider.analyze('核查接口规则')
    assert result.should_search and result.planned_questions[0].purpose=='DOCUMENTATION'
    assert result.planned_questions[0].source_strategy_hint==''
    assert analyzer.fallbacks==0


def test_inferred_site_query_requires_full_planner(monkeypatch):
    q=CompactQuestion(key='q1',query='site:github.com uv',intent='核查uv',aspect='compatibility')
    _,analyzer,provider=prepare(monkeypatch,plan(questions=[q]))
    assert provider.analyze('核查uv').reason=='primary'
    assert analyzer.fallbacks==1


def test_failed_full_planner_is_not_silently_no_search_or_retried(monkeypatch):
    _,analyzer,provider=prepare(monkeypatch,plan(requires_source_scope=True))
    def failed(*_args,**_kwargs):
        analyzer.fallbacks+=1
        return ResearchDecision(should_search=False,reason='检索决策未完成，本轮先基于现有上下文回答。')
    analyzer.analyze=failed
    with pytest.raises(RuntimeError):
        provider.analyze('请核查，仅限指定来源')
    assert analyzer.fallbacks==1


def test_discovery_keeps_identifiers_but_defers_phrase_verification():
    from planner_api.research.laya_provider import discovery_constraints
    from planner_api.research.schemas import ResearchQueryConstraints
    original=ResearchQueryConstraints(entities=['SQLite','WAL'],strict_terms=['WAL','3.14','600元','读写并行'])
    assert discovery_constraints(original).strict_terms==['WAL','3.14','600元']
    assert original.strict_terms==['WAL','3.14','600元','读写并行']


def test_open_official_preference_can_read_relevant_community_candidate():
    from planner_api.research.orchestrator import ResearchOrchestrator
    from planner_api.research.schemas import ResearchPacket,ResearchQueryConstraints,SearchHit,SearchResponse
    class Service:
        def search(self,request):
            self.request=request
            return SearchResponse(query=request.query,searchedAt='2026-10-13T00:00:00Z',engines=['bing'],
                results=[SearchHit(url='https://www.zhihu.com/question/12345/answer/67890',
                    title='SQLite WAL concurrency practical observations',
                    snippet='SQLite WAL concurrency supports concurrent readers with a single writer. User reproduction found lock contention.',
                    engine='bing')])
    service=Service()
    orchestrator=ResearchOrchestrator(service=service,settings=Settings(_env_file=None))
    query=ResearchSubquery(query='SQLite WAL concurrency',query_intent='核查SQLite WAL读写并行',purpose='DOCUMENTATION',
        source_scope_mode='SOFT',source_strategy_hint='official',
        constraints=ResearchQueryConstraints(entities=['SQLite','WAL'],strict_terms=['WAL','读写并行']))
    packet=ResearchPacket(question='核查SQLite WAL读写并行')
    accepted=orchestrator._search_question(packet,query,query.query)
    assert any('SNIPPET_FALLBACK' not in item.reasons for item in accepted)
    assert 'site:' not in service.request.query
    assert packet.search_diagnostics[0].source_scope_dropped_count==0
    assert query.constraints.strict_terms==['WAL','读写并行']
