"""Reading admission must not silently undo successful semantic recall."""
import pytest

from planner_api.config import Settings
from planner_api.research.orchestrator import ResearchLimits, ResearchOrchestrator
from planner_api.research.schemas import ResearchPacket, ResearchSource, ResearchSubquery
from planner_api.research.sources import source_url_matches_profiles
from test_research_orchestrator import FakeResearchService


def candidate(**updates):
    return ResearchSource(url="https://museum.example.org/visit/reservation",
        canonical_url="https://museum.example.org/visit/reservation",
        title="参观预约须知", source_category="OTHER",
        ranking_reasons=["RELAXED_RELEVANCE_CANDIDATE"], **updates)


@pytest.mark.parametrize("mode", ["OFF", "BGE"])
def test_paraphrased_candidate_reaches_reader_without_becoming_certified_authority(mode):
    source = candidate()
    packet = ResearchPacket(question="参观预约", sources=[source])
    service = FakeResearchService({})
    runner = ResearchOrchestrator(service, settings=Settings(_env_file=None, research_rerank_mode=mode),
        limits=ResearchLimits(max_read_pages=1, max_pages_per_round=1))
    assert runner._read_selected(packet) == 1
    assert len(service.read_calls) == 1
    assert source.read_status == "SUCCEEDED"
    assert source.source_category == "OTHER" and not source.source_profiles
    assert source.evidence_level == "FULL_TEXT"  # Reading is not official attribution.


@pytest.mark.parametrize("mode", [False, True])
@pytest.mark.parametrize("reasons", [
    ["RELAXED_RELEVANCE_CANDIDATE", "SOURCE_PROFILE_MISMATCH"],
    ["SNIPPET_FALLBACK", "ENTITY_MISMATCH"],
    ["SNIPPET_FALLBACK", "SOURCE_PROFILE_MISMATCH"],
])
def test_soft_recall_does_not_admit_known_source_or_entity_mismatch(mode, reasons):
    source = candidate()
    source.ranking_reasons = reasons
    packet = ResearchPacket(question="参观预约", sources=[source])
    assert not ResearchOrchestrator._ordered_read_candidates(packet, enhanced=mode)


def test_accepted_candidate_still_obeys_read_budget_and_original_checks():
    first, second = candidate(), candidate()
    second.url = "https://museum.example.net/guide"
    second.canonical_url = str(second.url)
    packet = ResearchPacket(question="参观预约", sources=[first, second])
    service = FakeResearchService({})
    runner = ResearchOrchestrator(service, limits=ResearchLimits(max_read_pages=1, max_pages_per_round=1))
    assert runner._read_selected(packet) == 1
    assert len(service.read_calls) == 1 and second.read_status == "NOT_SELECTED"
    assert not packet.claims


@pytest.mark.parametrize("url", ["https://suzhou.bendibao.com/tour/123.shtm",
    "https://m.suzhou.bendibao.com/mip/123.shtm", "https://wenku.baidu.com/view/123.html"])
def test_third_party_city_guide_is_not_an_official_source_profile(url):
    assert not source_url_matches_profiles("https://suzhou.bendibao.com/tour/123.shtm", ["official_docs"])
    assert not source_url_matches_profiles("https://m.suzhou.bendibao.com/mip/123.shtm", ["official_docs"])
    source = candidate(matched_queries=["官方预约"])
    assert not source_url_matches_profiles(url, ["official_docs"])
    source.url = url
    source.canonical_url = str(source.url)
    query = ResearchSubquery(query="官方预约", query_role="AUTHORITATIVE", source_profiles=["official_docs"])
    packet = ResearchPacket(question="只看官方预约规则", sources=[source], subqueries=[query])
    assert not ResearchOrchestrator._ordered_read_candidates(packet)
    query.source_scope_mode = "SOFT"
    assert ResearchOrchestrator._ordered_read_candidates(packet) == [source]
