from planner_api.research.query_planner import preserve_query_constraints
from planner_api.research.schemas import ResearchQueryConstraints, ResearchSubquery
from planner_api.research.sources import official_documentation_domains, official_reference_urls
from planner_api.research.orchestrator import ResearchOrchestrator
from planner_api.research.schemas import ResearchPacket


def test_reordered_documentation_and_version_hints_do_not_pollute_search_query():
    question = ResearchSubquery(query="Python 3.14 asyncio TaskGroup documentation official",
        constraints=ResearchQueryConstraints(specifications=["3.14 版本", "official asyncio documentation"],
                                            entities=["Python 3.14", "TaskGroup", "asyncio"]))
    assert preserve_query_constraints(question) == question.query


def test_missing_hard_product_constraint_is_still_preserved():
    question = ResearchSubquery(query="Ollama memory requirements",
        constraints=ResearchQueryConstraints(strict_terms=["RTX 3050", "6GB"]))
    query = preserve_query_constraints(question)
    assert "RTX 3050" in query and "6GB" in query


def test_verified_publisher_scope_does_not_guess_unknown_projects():
    assert official_documentation_domains("Python 3.14 TaskGroup") == ("docs.python.org",)
    assert official_documentation_domains("UnknownProject TaskGroup") == ()
    assert official_reference_urls("Python 3.14 asyncio TaskGroup") == ("https://docs.python.org/3.14/library/asyncio-task.html",)
    assert official_reference_urls("UnknownProject asyncio") == ()


def test_model_reference_is_only_an_unverified_official_read_candidate():
    packet = ResearchPacket(question="Python TaskGroup", subqueries=[ResearchSubquery(
        query="Python TaskGroup", source_profiles=["official_docs"], reference_urls=[
            "https://docs.python.org/3.14/library/asyncio-task.html#task-groups",
            "https://docs.pythonlang.cn/3/library/asyncio-task.html",
            "http://127.0.0.1/private",
        ])])
    ResearchOrchestrator._seed_reference_sources(packet)
    ResearchOrchestrator._seed_reference_sources(packet)
    assert len(packet.sources) == 1
    assert packet.sources[0].read_status == "NOT_SELECTED"
    assert packet.sources[0].evidence_level == "UNVERIFIED"
    assert not packet.sources[0].snippet
    assert not packet.claims
