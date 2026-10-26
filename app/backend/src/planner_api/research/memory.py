from __future__ import annotations

from .schemas import ResearchPacket, ResearchRoundMemory


def _unique(items: list[str], limit: int) -> list[str]:
    return list(dict.fromkeys(item.strip() for item in items if item.strip()))[-limit:]


def refresh_research_memory(
    packet: ResearchPacket,
    *,
    new_passage_ids: list[str] | None = None,
    new_claim_ids: list[str] | None = None,
) -> None:
    """Fold one round into bounded, structured research memory."""
    memory = packet.memory
    memory.covered_aspects = _unique(packet.coverage.covered_aspects, 12)
    memory.unresolved_points = _unique(
        [*packet.coverage.uncovered_aspects, *packet.uncertain_points], 20
    )
    memory.conflict_ids = _unique(
        [
            conflict.conflict_id
            for conflict in packet.conflicts
            if conflict.resolution == "UNRESOLVED" and conflict.conflict_id
        ],
        10,
    )
    round_index = packet.coverage.rounds
    previous = next(
        (item for item in memory.recent_rounds if item.round_index == round_index), None
    )
    passage_ids = _unique(
        [*(previous.new_passage_ids if previous else []), *(new_passage_ids or [])], 20
    )
    claim_ids = _unique(
        [*(previous.new_claim_ids if previous else []), *(new_claim_ids or [])], 20
    )
    if not passage_ids and not claim_ids and previous is None and round_index == 0:
        return
    summary_parts = [
        f"第 {round_index} 轮：新增 {len(passage_ids)} 个证据片段、{len(claim_ids)} 条事实。"
    ]
    if memory.covered_aspects:
        summary_parts.append("已覆盖：" + "、".join(memory.covered_aspects[:6]))
    if memory.unresolved_points:
        summary_parts.append("待核实：" + "；".join(memory.unresolved_points[:3]))
    if memory.conflict_ids:
        summary_parts.append(f"未裁决冲突 {len(memory.conflict_ids)} 项。")
    row = ResearchRoundMemory(
        round_index=round_index,
        new_passage_ids=passage_ids,
        new_claim_ids=claim_ids,
        conflict_ids=memory.conflict_ids,
        covered_aspects=memory.covered_aspects,
        unresolved_points=memory.unresolved_points[:10],
        summary=" ".join(summary_parts)[:1200],
    )
    memory.recent_rounds = [
        item for item in memory.recent_rounds if item.round_index != round_index
    ][-5:] + [row]
