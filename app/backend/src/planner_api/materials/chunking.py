from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .parsers import ParsedLocation


@dataclass(frozen=True)
class MaterialChunk:
    sequence: int
    text: str
    section_title: str
    char_start: int
    char_end: int
    locator_details: list[dict[str, object]]


def chunk_material(
    text: str,
    *,
    max_chars: int = 1800,
    overlap_chars: int = 180,
    locations: tuple[ParsedLocation, ...] = (),
) -> list[MaterialChunk]:
    normalized = text.strip()
    if not normalized:
        return []
    if max_chars < 1 or overlap_chars < 0 or overlap_chars >= max_chars:
        raise ValueError("chunk size must be positive and overlap must be smaller than chunk size")

    chunks: list[MaterialChunk] = []
    heading_positions = [
        (match.start(), match.group(1).strip()[:300])
        for match in re.finditer(r"(?m)^#{1,6}\s+(.+)$", normalized)
    ]
    heading_index = 0
    current_section = ""
    start = 0
    while start < len(normalized):
        while start < len(normalized) and normalized[start].isspace():
            start += 1
        if start >= len(normalized):
            break
        end = min(start + max_chars, len(normalized))
        if end < len(normalized):
            boundary = _preferred_boundary(normalized, start, end)
            if boundary > start:
                end = boundary
        while end > start and normalized[end - 1].isspace():
            end -= 1
        if end <= start:
            end = min(start + max_chars, len(normalized))
        while (
            heading_index < len(heading_positions)
            and heading_positions[heading_index][0] <= start
        ):
            current_section = heading_positions[heading_index][1]
            heading_index += 1
        chunk_locations = _locations_for_range(locations, start, end)
        section_title = current_section or _section_title_from_locations(chunk_locations)
        chunks.append(
            MaterialChunk(
                sequence=len(chunks),
                text=normalized[start:end],
                section_title=section_title,
                char_start=start,
                char_end=end,
                locator_details=chunk_locations,
            )
        )
        if end >= len(normalized):
            break
        next_start = max(start + 1, end - overlap_chars)
        while next_start < end and normalized[next_start].isspace():
            next_start += 1
        start = next_start
    return chunks


def _locations_for_range(
    locations: tuple[ParsedLocation, ...], start: int, end: int
) -> list[dict[str, object]]:
    details: list[dict[str, object]] = []
    for location in locations:
        if location.char_start >= end or location.char_end <= start:
            continue
        detail = {
            **location.detail,
            "chunk_text_start": max(start, location.char_start) - start,
            "chunk_text_end": min(end, location.char_end) - start,
        }
        if detail.get("kind") == "pptx_text_shape":
            detail = {
                "kind": "pptx_slide",
                "slide": detail.get("slide"),
                "chunk_text_start": detail.get("chunk_text_start"),
                "chunk_text_end": detail.get("chunk_text_end"),
            }
        if detail not in details:
            details.append(detail)
    return _compact_locations(details)


def _compact_locations(details: list[dict[str, object]]) -> list[dict[str, object]]:
    compact: list[dict[str, object]] = []
    for detail in details:
        if detail.get("kind") == "pptx_slide" and compact:
            previous = compact[-1]
            if (
                previous.get("kind") == "pptx_slide"
                and previous.get("slide") == detail.get("slide")
            ):
                previous["chunk_text_start"] = min(
                    previous.get("chunk_text_start", 0), detail.get("chunk_text_start", 0)
                )
                previous["chunk_text_end"] = max(
                    previous.get("chunk_text_end", 0), detail.get("chunk_text_end", 0)
                )
                continue

        if detail.get("kind") == "pdf_page":
            compact.append(detail.copy())
            continue

        if detail.get("kind") == "xlsx_row" and compact:
            previous = compact[-1]
            if (
                previous.get("kind") == "xlsx_rows"
                and previous.get("sheet") == detail.get("sheet")
                and previous.get("row_end") == detail.get("row_start", 0) - 1
            ):
                previous["row_end"] = detail.get("row_end")
                previous["chunk_text_end"] = detail.get("chunk_text_end")
                previous["cell_refs"] = list(
                    dict.fromkeys(
                        [*previous.get("cell_refs", []), *detail.get("cell_refs", [])]
                    )
                )[:32]
                continue
            if (
                previous.get("kind") == "xlsx_row"
                and previous.get("sheet") == detail.get("sheet")
                and previous.get("row_end") == detail.get("row_start", 0) - 1
            ):
                compact[-1] = {
                    "kind": "xlsx_rows",
                    "sheet": detail.get("sheet"),
                    "row_start": previous.get("row_start"),
                    "row_end": detail.get("row_end"),
                    "chunk_text_start": previous.get("chunk_text_start"),
                    "chunk_text_end": detail.get("chunk_text_end"),
                    "cell_refs": list(
                        dict.fromkeys(
                            [*previous.get("cell_refs", []), *detail.get("cell_refs", [])]
                        )
                    )[:32],
                }
                continue
        if detail not in compact:
            compact.append(detail.copy())
    return compact


def _section_title_from_locations(details: list[dict[str, object]]) -> str:
    if not details:
        return ""
    detail = details[0]
    heading_path = detail.get("heading_path")
    if isinstance(heading_path, list) and heading_path:
        return str(heading_path[-1])[:300]
    if detail.get("sheet"):
        return str(detail["sheet"])[:300]
    if detail.get("kind") == "pptx_slide":
        return f"Slide {detail.get('slide')}"
    return ""


def _preferred_boundary(text: str, start: int, end: int) -> int:
    floor = start + int((end - start) * 0.65)
    candidates: list[int] = []
    for separator in ("\n\n", "\n", "。", "！", "？", ". ", "! ", "? ", "；", "; "):
        boundary = text.rfind(separator, floor, end)
        if boundary >= floor:
            candidates.append(boundary + len(separator))
    return max(candidates, default=end)
