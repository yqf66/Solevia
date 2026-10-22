from planner_api.ai import FakeClarificationProvider
from planner_api.execution_service import ExecutionService
from planner_api.goal_titles import apply_goal_title, initial_goal_title
from planner_api.models import Goal
from planner_api.schemas import RequirementAnalysis
from planner_api.service import ClarificationService


def test_short_title_is_applied_in_existing_analysis_without_extra_provider_call(session_factory):
    raw = "我想系统学习 Agent 和大模型应用开发，准备找相关实习。"

    class Provider(FakeClarificationProvider):
        calls = 0

        def analyze(self, context, assistant_message):
            self.calls += 1
            return super().analyze(context, assistant_message).model_copy(update={"goal_title": "Agent 开发与实习准备"})

    with session_factory() as session:
        provider = Provider()
        workspace = ClarificationService(session, provider).create_goal(raw)
        goal = session.get(Goal, workspace.goal_id)
        assert provider.calls == 1
        assert goal.title == "Agent 开发与实习准备" and goal.raw_input == raw
        apply_goal_title(session, goal, "后续分析不得反复改名")
        assert goal.title == "Agent 开发与实习准备"
        # Even explicitly choosing the original name must count as user intent.
        ExecutionService(session).rename_goal(goal.id, initial_goal_title(raw))
        apply_goal_title(session, goal, "不应覆盖用户名称")
        assert goal.title == initial_goal_title(raw)


def test_legacy_custom_names_and_invalid_optional_suggestions_do_not_break_analysis(session_factory):
    with session_factory() as session:
        workspace = ClarificationService(session, FakeClarificationProvider()).create_goal("我想学习园艺")
        goal = session.get(Goal, workspace.goal_id)
        goal.title = "我的阳台"
        apply_goal_title(session, goal, "园艺入门")
        assert goal.title == "我的阳台"
        goal.title = initial_goal_title(goal.raw_input)
        for suggestion in [None, "", "x" * 33, "M1-W1-D1"]:
            apply_goal_title(session, goal, suggestion)
        assert goal.title == initial_goal_title(goal.raw_input)
    assert "goal_title" not in RequirementAnalysis.model_json_schema()["required"]
