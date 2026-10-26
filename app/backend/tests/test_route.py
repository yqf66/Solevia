from sqlalchemy import select

from planner_api.ai import ProviderUnavailableError
from planner_api.models import Goal, RequirementVersion, RouteVersion
from planner_api.route_ai import FakeRouteProvider
from planner_api.route_explanation_ai import FakeRouteExplanationProvider
from planner_api.route_service import RouteService
from planner_api.schemas import RouteDraftResult


def _confirmed_requirement(client) -> dict:
    from test_clarification import answer_three_rounds, create_goal

    workspace = answer_three_rounds(client, create_goal(client))
    selected = client.put(
        f"/api/goals/{workspace['goal_id']}/planning-approach",
        json={"planning_mode": "COMMITTED"},
    )
    assert selected.status_code == 200
    schedule = client.put(
        f"/api/goals/{workspace['goal_id']}/schedule-type",
        json={"schedule_type": "RHYTHM"},
    )
    assert schedule.status_code == 200
    response = client.post(f"/api/goals/{workspace['goal_id']}/requirements/confirm")
    assert response.status_code == 200
    return response.json()


def test_confirmed_requirements_generate_first_level_route(client, session_factory) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]

    response = client.post(f"/api/goals/{goal_id}/route/draft")

    assert response.status_code == 200
    workspace = response.json()
    assert workspace["status"] == "ROUTE_REVIEW"
    assert workspace["requirement_version"] == 1
    assert workspace["route"]["version"] == 1
    assert workspace["route"]["revision"] == 1
    assert workspace["route"]["status"] == "DRAFT"
    assert len(workspace["route"]["stages"]) == 3
    assert workspace["route"]["stages"][1]["depends_on"] == [1]
    assert workspace["route"]["stages"][1]["options"][0]["recommended"] is True
    assert workspace["route"]["source_document"].startswith("# 路线概览")
    assert workspace["route"]["compiler_report"]["status"] == "VALIDATED"

    with session_factory() as session:
        stored = session.scalar(select(RouteVersion).where(RouteVersion.goal_id == goal_id))
        assert stored is not None
        assert stored.source_document == workspace["route"]["source_document"]
        assert stored.compiled_snapshot_json["summary"] == workspace["route"]["summary"]


def test_planning_approach_is_confirmed_before_route_and_changes_route_strategy(client) -> None:
    from test_clarification import answer_three_rounds, create_goal

    workspace = answer_three_rounds(client, create_goal(client))
    goal_id = workspace["goal_id"]
    assert client.post(f"/api/goals/{goal_id}/requirements/confirm").status_code == 409
    selected = client.put(
        f"/api/goals/{goal_id}/planning-approach",
        json={"planning_mode": "EXPLORATORY"},
    )
    assert selected.status_code == 200
    assert selected.json()["selected_planning_mode"] == "EXPLORATORY"
    schedule = client.put(
        f"/api/goals/{goal_id}/schedule-type",
        json={"schedule_type": "OUTCOME"},
    )
    assert schedule.status_code == 200
    assert schedule.json()["selected_schedule_type"] == "OUTCOME"
    assert client.post(f"/api/goals/{goal_id}/requirements/confirm").status_code == 200
    route = client.post(f"/api/goals/{goal_id}/route/draft").json()
    assert route["planning_mode"] == "EXPLORATORY"
    assert route["route"]["planning_mode"] == "EXPLORATORY"
    assert "先试试看" in route["route"]["summary"] or "体验" in route["route"]["summary"]


def test_route_draft_endpoint_is_idempotent(client) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]

    first = client.post(f"/api/goals/{goal_id}/route/draft").json()
    second = client.post(f"/api/goals/{goal_id}/route/draft").json()

    assert second["route"]["id"] == first["route"]["id"]
    assert second["route"]["revision"] == 1


def test_route_feedback_preserves_draft_history(client, session_factory) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    first = client.post(f"/api/goals/{goal_id}/route/draft").json()

    response = client.post(
        f"/api/goals/{goal_id}/route/revise",
        json={"content": "第二阶段对我来说太大，请缩小范围并保持其他阶段不变。"},
    )

    assert response.status_code == 200
    revised = response.json()
    assert revised["route"]["version"] == 1
    assert revised["route"]["revision"] == 2
    assert revised["route"]["id"] != first["route"]["id"]
    assert revised["route"]["change_summary"]
    assert [message["role"] for message in revised["messages"]] == [
        "ASSISTANT",
        "USER",
        "ASSISTANT",
    ]
    with session_factory() as session:
        versions = list(
            session.scalars(
                select(RouteVersion)
                .where(RouteVersion.goal_id == goal_id)
                .order_by(RouteVersion.revision)
            )
        )
    assert [version.status for version in versions] == ["SUPERSEDED", "DRAFT"]


def test_route_explanation_adds_conversation_without_creating_revision(
    client, session_factory
) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    original = client.post(f"/api/goals/{goal_id}/route/draft").json()

    response = client.post(
        f"/api/goals/{goal_id}/route/explain",
        json={"content": "为什么第二阶段要放在这里？"},
    )

    assert response.status_code == 200
    explained = response.json()
    assert explained["route"]["id"] == original["route"]["id"]
    assert explained["route"]["revision"] == 1
    assert explained["messages"][-2]["role"] == "USER"
    assert explained["messages"][-1]["role"] == "ASSISTANT"
    with session_factory() as session:
        versions = list(
            session.scalars(select(RouteVersion).where(RouteVersion.goal_id == goal_id))
        )
    assert len(versions) == 1


def test_retrying_failed_route_feedback_reuses_the_failed_exchange(client, session_factory) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    client.post(f"/api/goals/{goal_id}/route/draft")

    class FailOnceRouteProvider:
        def __init__(self) -> None:
            self.calls = 0

        def draft_route(self, context):
            self.calls += 1
            if self.calls == 1:
                raise ProviderUnavailableError(
                    "模型暂时没有返回可用路线，请稍后重试。",
                    diagnostic="temporary test failure",
                )
            return FakeRouteProvider().draft_route(context)

    provider = FailOnceRouteProvider()
    feedback = "第二阶段太难，请缩小范围。"
    with session_factory() as session:
        service = RouteService(session, provider, FakeRouteExplanationProvider())
        failed = service.revise_route(goal_id, feedback)
        assert len(failed.messages) == 3
        assert failed.messages[-1].generation_status == "FAILED"
        assert failed.messages[-1].metadata["intent"] == "REVISE"

        retried = service.revise_route(goal_id, feedback)
        assert len(retried.messages) == 3
        assert retried.messages[-1].generation_status == "SUCCEEDED"
        assert retried.route is not None
        assert retried.route.revision == 2


def test_route_with_optional_followups_can_continue_with_current_assumptions(
    client, session_factory
) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    drafted = client.post(f"/api/goals/{goal_id}/route/draft").json()
    with session_factory() as session:
        route = session.get(RouteVersion, drafted["route"]["id"])
        assert route is not None
        route.open_questions_json = ["还需要确认一个关键选择"]
        session.commit()

    response = client.post(f"/api/goals/{goal_id}/route/confirm")

    assert response.status_code == 200
    assert response.json()["status"] == "PLAN_DRAFTING"


def test_only_user_confirmation_publishes_route(client, session_factory) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    client.post(f"/api/goals/{goal_id}/route/draft")

    response = client.post(f"/api/goals/{goal_id}/route/confirm")

    assert response.status_code == 200
    workspace = response.json()
    assert workspace["status"] == "PLAN_DRAFTING"
    assert workspace["route"]["status"] == "CONFIRMED"
    with session_factory() as session:
        goal = session.get(Goal, goal_id)
        assert goal is not None
        assert goal.active_route_version_id == workspace["route"]["id"]

    second_confirm = client.post(f"/api/goals/{goal_id}/route/confirm")
    assert second_confirm.status_code == 409


def test_route_is_blocked_before_requirement_confirmation(client) -> None:
    from test_clarification import create_goal

    workspace = create_goal(client)

    response = client.post(f"/api/goals/{workspace['goal_id']}/route/draft")

    assert response.status_code == 409


def test_browsing_requirements_preserves_route_until_a_real_change_is_confirmed(
    client, session_factory
) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    route = client.post(f"/api/goals/{goal_id}/route/draft").json()

    response = client.post(f"/api/goals/{goal_id}/requirements/reopen")

    assert response.status_code == 200
    reopened = response.json()
    assert reopened["status"] == "ROUTE_REVIEW"
    assert reopened["requirement_draft"]["version"] == 1
    assert reopened["requirement_draft"]["status"] == "CONFIRMED"
    assert reopened["downstream_route_preserved"] is True
    assert reopened["requirements_changed_since_route"] is False

    objective = next(fact for fact in reopened["facts"] if fact["field_key"] == "objective")
    changed_response = client.patch(
        f"/api/goals/{goal_id}/facts/{objective['id']}",
        json={"action": "REPLACE", "value": "完成一个更聚焦的 Agent 项目"},
    )
    assert changed_response.status_code == 200
    changed = changed_response.json()
    assert changed["status"] == "REQUIREMENT_REVIEW"
    assert changed["requirement_draft"]["version"] == 2
    assert changed["requirements_changed_since_route"] is True

    with session_factory() as session:
        requirements = list(
            session.scalars(
                select(RequirementVersion)
                .where(RequirementVersion.goal_id == goal_id)
                .order_by(RequirementVersion.version)
            )
        )
        previous_route = session.get(RouteVersion, route["route"]["id"])
    assert [item.status for item in requirements] == ["CONFIRMED", "DRAFT"]
    assert previous_route is not None
    assert previous_route.status == "DRAFT"

    confirmed = client.post(f"/api/goals/{goal_id}/requirements/confirm")
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "ROUTE_DRAFTING"
    with session_factory() as session:
        previous_route = session.get(RouteVersion, route["route"]["id"])
        assert previous_route is not None
        assert previous_route.status == "SUPERSEDED"


def test_user_can_discard_requirement_changes_and_continue_preserved_route(client) -> None:
    requirement = _confirmed_requirement(client)
    goal_id = requirement["goal_id"]
    original_objective = next(
        fact["value"] for fact in requirement["facts"] if fact["field_key"] == "objective"
    )
    route = client.post(f"/api/goals/{goal_id}/route/draft").json()
    objective = next(fact for fact in requirement["facts"] if fact["field_key"] == "objective")
    client.patch(
        f"/api/goals/{goal_id}/facts/{objective['id']}",
        json={"action": "REPLACE", "value": "一个临时修改的目标"},
    )

    response = client.post(f"/api/goals/{goal_id}/requirements/changes/discard")

    assert response.status_code == 200
    restored = response.json()
    assert restored["status"] == "ROUTE_REVIEW"
    assert restored["requirement_draft"]["version"] == 1
    assert restored["requirement_draft"]["status"] == "CONFIRMED"
    assert restored["requirements_changed_since_route"] is False
    assert (
        next(fact["value"] for fact in restored["facts"] if fact["field_key"] == "objective")
        == original_objective
    )
    current_route = client.get(f"/api/goals/{goal_id}/route").json()
    assert current_route["route"]["id"] == route["route"]["id"]


def test_route_schema_rejects_forward_dependencies() -> None:
    payload = {
        "assistant_message": "这里是一条一级路线。",
        "summary": "通过三个阶段完成目标并验证最终成果。",
        "stages": [
            {
                "order": 1,
                "title": "第一阶段",
                "purpose": "建立后续推进需要使用的清晰起点。",
                "topics": ["基线"],
                "outputs": ["基线清单"],
                "depends_on": [2],
            },
            {
                "order": 2,
                "title": "第二阶段",
                "purpose": "完成目标所需的主要工作和中间成果。",
                "topics": ["核心工作"],
                "outputs": ["中间成果"],
                "depends_on": [1],
            },
            {
                "order": 3,
                "title": "第三阶段",
                "purpose": "按照成功标准检查并完成最终交付。",
                "topics": ["验证"],
                "outputs": ["最终成果"],
                "depends_on": [2],
            },
        ],
        "tradeoffs": [],
        "assumptions": [],
        "open_questions": [],
        "change_summary": [],
    }

    try:
        RouteDraftResult.model_validate(payload)
    except ValueError:
        pass
    else:
        raise AssertionError("Forward dependency should be rejected")
