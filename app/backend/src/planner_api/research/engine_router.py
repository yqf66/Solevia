from __future__ import annotations

from dataclasses import dataclass

SUPPORTED_ENGINES = (
    "bing",
    "duckduckgo",
    "startpage",
    "baidu",
    "sogou",
    "csdn",
    "juejin",
    "linuxdo",
    "exa",
    "brave",
)

DEFAULT_ENGINE_POOL = (
    "bing",
    "duckduckgo",
)

ENGINE_CATALOG = {
    "bing": {"capabilities": ["general", "current", "official"], "languages": ["zh", "en"]},
    "duckduckgo": {"capabilities": ["general", "privacy"], "languages": ["en", "zh"]},
    "startpage": {"capabilities": ["general", "privacy"], "languages": ["en", "zh"]},
    "baidu": {"capabilities": ["general", "chinese"], "languages": ["zh"]},
    "sogou": {"capabilities": ["general", "chinese"], "languages": ["zh"]},
    "csdn": {"capabilities": ["chinese_technology"], "languages": ["zh"]},
    "juejin": {"capabilities": ["chinese_development"], "languages": ["zh"]},
    "linuxdo": {"capabilities": ["developer_community"], "languages": ["zh", "en"]},
    "exa": {"capabilities": ["semantic", "research"], "languages": ["en", "zh"]},
    "brave": {"capabilities": ["general", "privacy"], "languages": ["en", "zh"]},
}

_PURPOSE_PREFERENCES: dict[str, tuple[str, ...]] = {
    "ACADEMIC": ("exa", "bing", "duckduckgo", "startpage"),
    "CURRENT": ("bing", "sogou", "baidu", "duckduckgo", "startpage"),
    "DOCUMENTATION": ("bing", "duckduckgo", "csdn", "juejin", "baidu"),
    "COMPARISON": ("bing", "duckduckgo", "startpage", "baidu"),
    "RISK": ("bing", "duckduckgo", "startpage", "baidu"),
    "GENERAL": ("bing", "duckduckgo", "baidu", "csdn"),
}


def parse_engine_pool(value: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if isinstance(value, str):
        values = value.split(",")
    else:
        values = list(value or ())
    normalized = []
    for item in values:
        engine = str(item).strip().lower()
        # Open-WebSearch's Bing adapter already uses cn.bing.com. Accept the
        # explicit alias in configuration/model output without inventing a
        # second provider that would be routed differently.
        if engine in {"bing-cn", "bing_cn", "cn-bing", "cn_bing"}:
            engine = "bing"
        if engine in SUPPORTED_ENGINES and engine not in normalized:
            normalized.append(engine)
    return tuple(normalized) or DEFAULT_ENGINE_POOL


@dataclass(frozen=True)
class EngineRouter:
    """Select a small, purpose-aware engine set for one search request."""

    pool: tuple[str, ...] = DEFAULT_ENGINE_POOL
    max_engines: int = 3

    def select(
        self,
        *,
        purpose: str = "GENERAL",
        failed: set[str] | None = None,
        candidates: list[str] | tuple[str, ...] | None = None,
        strict_candidates: bool = False,
    ) -> list[str]:
        failed = {item.casefold() for item in (failed or set())}
        configured = parse_engine_pool(self.pool)
        previous = parse_engine_pool(candidates) if candidates else ()
        # Candidates are preferences, not permission to bypass the configured
        # health/policy pool. This prevents a failed/unconfigured engine from
        # being reintroduced by a source profile or model output.
        allowed = set(previous if strict_candidates and previous else configured)
        preferences = _PURPOSE_PREFERENCES.get(purpose.upper(), _PURPOSE_PREFERENCES["GENERAL"])

        selected: list[str] = []
        ordered = (
            (*preferences, *previous)
            if strict_candidates and previous
            else (*preferences, *previous, *configured)
        )
        for engine in ordered:
            if engine in allowed and engine not in failed and engine not in selected:
                selected.append(engine)
            if len(selected) >= max(1, self.max_engines):
                break
        if not selected and strict_candidates and previous:
            return self.select(purpose=purpose, failed=failed, candidates=None)
        return selected
