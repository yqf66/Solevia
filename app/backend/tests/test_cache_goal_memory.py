from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import delete, select

from planner_api.config import Settings
from planner_api.goal_memory import save_memories, recall_memory
from planner_api.knowledge.integration import merge_context, reauthorize_cache, selected_cache, recall_context
from planner_api.knowledge.search import KnowledgeSearch, cached_context
from planner_api.knowledge.store import KnowledgeStore
from planner_api.models import Conversation, Goal, GoalLongTermMemory, KnowledgeDocument, Message, utc_now
from planner_api.planning_context import build_requirement_memory
from planner_api.research.orchestrator import ResearchLimits, ResearchOrchestrator
from planner_api.research.schemas import ResearchDecision, ResearchPacket, ResearchSubquery, SearchResponse
from planner_api.schemas import LongTermMemoryCandidate


@pytest.fixture
def memory_env(session_factory, monkeypatch):
    settings = Settings(_env_file=None, knowledge_enabled=True, knowledge_vectors_enabled=False,
                        research_rerank_mode="OFF")
    monkeypatch.setattr("planner_api.goal_memory.get_settings", lambda: settings)
    with session_factory() as session:
        goal, other = Goal(title="SQLite app", raw_input="SQLite app"), Goal(title="other", raw_input="other")
        session.add_all([goal, other]); session.flush()
        conversation = Conversation(goal_id=goal.id, phase="CLARIFICATION")
        session.add(conversation); session.flush()
        message = Message(conversation_id=conversation.id, sequence=1, role="USER",
                          content="我的数据库必须离线运行，SQLite 写入只在主线程。")
        session.add(message); session.commit()
        yield session, settings, goal, other, conversation, message


def candidate(**kwargs):
    return LongTermMemoryCandidate(memory_key="database_execution",
        content=kwargs.pop("content", "SQLite 离线运行，写入在主线程"),
        evidence_quote=kwargs.pop("evidence_quote", "SQLite 写入只在主线程"), **kwargs)


def test_memory_is_grounded_private_cross_phase_and_survives_vector_eviction(memory_env):
    session, settings, goal, other, conv, msg = memory_env
    save_memories(session, conv, [candidate()], source_message=msg)
    session.commit()
    session.execute(delete(KnowledgeDocument)); session.commit()
    for phase in ("ROUTE", "PLAN", "EXECUTION"):
        session.add(Conversation(goal_id=goal.id, phase=phase))
    session.commit()
    memories = recall_memory(session, goal.id, "SQLite 写入", settings=settings)
    assert len(memories) == 1 and memories[0]["source_message_id"] == msg.id
    assert memories[0]["source_phase"] == "CLARIFICATION"
    assert memories[0]["authority"] == "USER_CONTEXT_NOT_EXTERNAL_EVIDENCE"
    assert build_requirement_memory(session, goal.id, "SQLite 写入")["long_term_memory"] == memories
    assert not recall_memory(session, other.id, "SQLite 写入", settings=settings)
    save_memories(session, conv, [candidate(evidence_quote="助手推断的长期偏好")], source_message=msg)
    assert len(list(session.scalars(select(GoalLongTermMemory)))) == 1


def test_memory_correction_retraction_and_retry_do_not_resurrect_old_content(memory_env):
    session, settings, goal, other, conv, msg = memory_env
    save_memories(session, conv, [candidate()], source_message=msg); session.commit()
    newer = Message(conversation_id=conv.id, sequence=3, role="USER",
        content="改为 SQLite 专用写入队列，不再限定主线程", created_at=utc_now()+timedelta(seconds=1))
    session.add(newer); session.flush()
    update = candidate(content="SQLite 使用专用写入队列", evidence_quote="SQLite 专用写入队列")
    save_memories(session, conv, [update], source_message=newer); session.commit()
    save_memories(session, conv, [candidate()], source_message=msg); session.commit()
    result = recall_memory(session, goal.id, "SQLite 写入", settings=settings)
    assert result[0]["content"] == update.content
    save_memories(session, conv, [candidate(content="撤回", evidence_quote="不再限定主线程", operation="RETRACT")], source_message=newer)
    session.commit()
    assert not recall_memory(session, goal.id, "SQLite", settings=settings)


def test_user_memory_is_never_external_evidence_even_if_shared_flag_changes(memory_env):
    session, settings, goal, other, conv, msg = memory_env
    save_memories(session, conv, [candidate()], source_message=msg); session.commit()
    doc = session.scalar(select(KnowledgeDocument))
    doc.shared = True; session.commit()
    assert not KnowledgeSearch(session, settings).search(other.id, "SQLite", manual=True,
        kinds={"GOAL_MEMORY"})["hits"]
    assert not KnowledgeSearch(session, settings).search(goal.id, "SQLite", manual=True)["hits"]


def test_cache_selection_is_id_based_and_reauthorized_before_merge(memory_env):
    session, settings, goal, other, conv, msg = memory_env
    text = "SQLite WAL permits only one writer at a time."
    doc = KnowledgeStore(session, settings).put(goal.id, kind="WEB", uri="https://sqlite.org/wal.html",
        title="SQLite WAL", body=text, pieces=[{"text":text,"start":0,"end":len(text)}],
        shared=True, metadata={"coverage":"FULL_DOCUMENT"})
    session.commit()
    cached = cached_context(session, settings, goal.id, "SQLite WAL writer", track_access=False)
    evidence_id = cached["selected_evidence"][0]["evidence_id"]
    packet = ResearchPacket(question="SQLite WAL and latest release", decision=ResearchDecision(
        should_search=True, cache_coverage="PARTIAL", cached_evidence_ids=[evidence_id, "kb:invented"],
        cache_missing_aspects=["latest version"]))
    chosen = selected_cache(packet, {"cached_evidence":cached})
    assert len(chosen["selected_evidence"]) == 1
    fresh={"evidence_mode":"SPANS","research_status":"PARTIAL","selected_evidence":[],
           "research_coverage":{"read_pages":1,"searched_queries":1}}
    merged=merge_context(fresh, reauthorize_cache(session,settings,goal.id,chosen,phase="PLAN"))
    assert merged["selected_evidence"][0]["quote"] == text
    assert merged["research_status"] == "PARTIAL"
    assert merged["research_coverage"]["read_pages"] == 1
    doc.current=False;session.commit()
    assert not reauthorize_cache(session,settings,goal.id,chosen,phase="PLAN")


def test_parallel_queries_overlap_io_but_keep_packet_counts_and_order():
    barrier=Barrier(2)
    class Provider:
        def search(self, request):
            barrier.wait(timeout=3)
            return SearchResponse(query=request.query,results=[],engines=["bing"],result_counts={"bing":0},searchedAt="now")
    orchestrator=ResearchOrchestrator(settings=Settings(_env_file=None,research_rerank_mode="OFF"))
    orchestrator.service=Provider()
    packet=ResearchPacket(question="SQLite and Python",decision=ResearchDecision(should_search=True))
    queries=[ResearchSubquery(key="q1",query="SQLite WAL"),ResearchSubquery(key="q2",query="Python TaskGroup")]
    packet.subqueries=queries
    orchestrator._search_query_batch(packet,[(q,q.query,False) for q in queries])
    assert packet.coverage.searched_queries==2
    assert packet.coverage.empty_queries==2
    assert [q.key for q in packet.search_diagnostics]==["q1","q2"]


def test_candidate_presence_does_not_become_sufficient_coverage():
    packet=ResearchPacket(question="Ruff formatter",decision=ResearchDecision(should_search=True))
    assert not selected_cache(packet,{"cached_evidence":{"selected_evidence":[{"evidence_id":"kb:museum"}]}})


def test_vector_projection_failure_does_not_lose_durable_memory(memory_env, monkeypatch):
    session, settings, goal, other, conv, msg = memory_env
    def fail(*args, **kwargs):
        raise RuntimeError("optional vector cache unavailable")
    monkeypatch.setattr(KnowledgeStore,"put",fail)
    save_memories(session,conv,[candidate()],source_message=msg);session.commit()
    assert recall_memory(session,goal.id,"SQLite",settings=settings)[0]["evidence_quote"] in msg.content


def test_empty_model_memory_output_preserves_previous_memory(memory_env):
    session, settings, goal, other, conv, msg = memory_env
    save_memories(session,conv,[candidate()],source_message=msg);session.commit()
    save_memories(session,conv,[],source_message=msg)
    assert len(recall_memory(session,goal.id,"SQLite",settings=settings))==1


def test_candidate_and_memory_recall_overlap_without_model_calls(memory_env, monkeypatch):
    session, settings, goal, other, conv, msg = memory_env
    barrier=Barrier(2)
    def cache(*args,**kwargs):
        barrier.wait(timeout=3)
        assert kwargs["track_access"] is False
        return {"test_candidate":True}
    def memory(*args,**kwargs):
        barrier.wait(timeout=3)
        return [{"memory_key":"key"}]
    monkeypatch.setattr("planner_api.knowledge.integration.cached_context",cache)
    monkeypatch.setattr("planner_api.goal_memory.recall_memory",memory)
    result=recall_context(session.get_bind(),settings,goal.id,"SQLite")
    assert result["cached_evidence"]=={"test_candidate":True}
    assert result["long_term_memory"]==[{"memory_key":"key"}]


def test_optional_recall_timeout_is_unknown_and_does_not_block_network(memory_env, monkeypatch):
    session, settings, goal, other, conv, msg = memory_env
    class Future:
        def result(self,timeout):
            raise TimeoutError()
        def cancel(self):
            return False
    class Pool:
        def submit(self,fn):
            return Future()
    monkeypatch.setattr("planner_api.knowledge.integration._recall_pool",Pool())
    result=recall_context(session.get_bind(),settings,goal.id,"SQLite")
    assert not result["cached_evidence"]
    assert set(result["recall_diagnostics"].values())=={"TIMED_OUT_UNKNOWN"}


def test_cache_complete_still_requires_original_ids_and_explicit_refresh_still_searches():
    class Decision:
        def analyze(self, question, *, context=None):
            return ResearchDecision(should_search=False,cache_coverage="SUFFICIENT",cached_evidence_ids=["kb:1"])
    class NoSearch:
        def search(self, request):
            raise AssertionError("stable cache must not call network")
    context={"cached_evidence":{"selected_evidence":[{"evidence_id":"kb:1","source_url":"https://sqlite.org/wal.html"}],"knowledge_cache":{}}}
    orchestrator=ResearchOrchestrator(service=NoSearch(),decision_provider=Decision(),
        settings=Settings(_env_file=None,research_rerank_mode="OFF"))
    packet=orchestrator.research("请查 SQLite WAL 的已有规则",enabled=True,decision_context=context)
    assert packet.status=="SKIPPED" and packet.cache_context["selected_evidence"]
    assert packet.coverage.searched_queries==0
    calls=[]
    class Search:
        def search(self, request):
            calls.append(request)
            return SearchResponse(query=request.query,searchedAt="now")
    orchestrator=ResearchOrchestrator(service=Search(),decision_provider=Decision(),
        settings=Settings(_env_file=None,research_rerank_mode="OFF"),
        limits=ResearchLimits(max_rounds=1,max_queries=1,max_read_pages=1))
    packet=orchestrator.research("请联网核对 SQLite 最新版本",enabled=True,decision_context=context)
    assert calls and packet.coverage.searched_queries==1
