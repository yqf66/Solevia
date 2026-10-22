"""Short display names without changing the user's complete requirement."""
import re

from sqlalchemy import select

from planner_api.models import Conversation, Message


def initial_goal_title(raw_input: str) -> str:
    text = re.sub(r"^(?:我想要?|我希望|我打算|我要|请帮我|帮我)\s*", "", raw_input.strip())
    text = re.split(r"[。！？\n]", text, maxsplit=1)[0]
    if len(text) > 28:
        first_clause = re.split(r"[，,；;]", text, maxsplit=1)[0]
        text = first_clause if 4 <= len(first_clause) <= 28 else text[:27] + "…"
    return text or "新的目标"


def title_message(session, goal_id):
    return session.scalar(select(Message).join(Conversation).where(
        Conversation.goal_id == goal_id, Conversation.phase == "CLARIFICATION",
        Message.role == "USER",
    ).order_by(Message.created_at, Message.sequence, Message.id).limit(1)
        .execution_options(populate_existing=True))


def apply_goal_title(session, goal, suggestion: str | None) -> None:
    if not isinstance(suggestion, str):
        return
    title = re.sub(r"\s+", " ", suggestion).strip().strip('"“”')
    if not title or len(title) > 32 or re.fullmatch(r"(?:M\d+-)?W\d+-D\d+", title, re.I):
        return
    source = title_message(session, goal.id)
    if source is None or (source.metadata_json or {}).get("goal_title_origin") in {"USER", "MODEL"}:
        return
    # Existing custom names, including older versions without provenance, win.
    legacy = goal.raw_input.strip().split("。", 1)[0][:60]
    if goal.title not in {legacy, initial_goal_title(goal.raw_input)}:
        return
    goal.title = title
    source.metadata_json = {**(source.metadata_json or {}), "goal_title_origin": "MODEL"}


def mark_user_title(session, goal_id: str) -> None:
    source = title_message(session, goal_id)
    if source is not None:
        source.metadata_json = {**(source.metadata_json or {}), "goal_title_origin": "USER"}
