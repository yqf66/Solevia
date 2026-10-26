from planner_api.research.schemas import ResearchPacket


def test_legacy_research_packet_checkpoint_remains_readable() -> None:
    legacy_checkpoint = {
        "run_id": "legacy-run",
        "question": "核对当前部署要求",
        "decision": {
            "should_search": True,
            "research_questions": ["官方部署要求"],
            "reason": "需要外部资料",
        },
        "subqueries": [{"query": "官方部署要求", "purpose": "DOCUMENTATION"}],
        "sources": [],
        "claims": [],
        "conflicts": [],
        "uncertain_points": [],
        "coverage": {
            "rounds": 1,
            "searched_queries": 1,
            "successful_queries": 1,
        },
        "search_diagnostics": [
            {
                "query": "官方部署要求",
                "status": "SUCCEEDED",
                "engines": ["bing"],
            }
        ],
        "stop_reason": None,
        "status": "PARTIAL",
    }

    packet = ResearchPacket.model_validate(legacy_checkpoint)

    assert packet.run_id == "legacy-run"
    assert packet.decision is not None
    assert packet.decision.research_questions == ["官方部署要求"]
    assert packet.coverage.searched_queries == 1
    assert packet.search_diagnostics[0].status == "SUCCEEDED"
    assert packet.gaps == []
    assert packet.memory.strategy_attempts == []
    assert packet.memory.consecutive_low_yield_rounds == 0
    assert packet.model_dump(mode="json")["question"] == "核对当前部署要求"


def test_checkpoint_without_optional_legacy_fields_gets_safe_defaults() -> None:
    packet = ResearchPacket.model_validate(
        {
            "question": "只检查问题是否需要联网",
            "status": "DISABLED",
        }
    )

    assert packet.decision is None
    assert packet.subqueries == []
    assert packet.sources == []
    assert packet.claims == []
    assert packet.conflicts == []
    assert packet.search_diagnostics == []
    assert packet.gaps == []
    assert packet.memory.strategy_attempts == []
