from __future__ import annotations

import re
from dataclasses import dataclass

from .query_planner import project_name
from .schemas import ResearchBrief, ResearchSubquery


@dataclass(frozen=True)
class SourceProfile:
    id: str
    domains: tuple[str, ...]
    category: str
    trust_level: str
    purposes: tuple[str, ...]
    query_prefix: str = ""


SOURCE_PROFILES: dict[str, SourceProfile] = {
    "community": SourceProfile(
        "community",
        (),
        "COMMUNITY_EXPERIENCE",
        "SECONDARY",
        ("GENERAL", "CURRENT", "COMPARISON", "RISK"),
    ),
    "github": SourceProfile(
        "github",
        ("github.com", "raw.githubusercontent.com"),
        "REPOSITORY",
        "PRIMARY",
        ("DOCUMENTATION", "COMPARISON", "RISK"),
    ),
    "official_docs": SourceProfile(
        "official_docs",
        (),
        "DOCUMENTATION",
        "PRIMARY",
        ("DOCUMENTATION", "CURRENT"),
    ),
    "arxiv": SourceProfile(
        "arxiv",
        ("arxiv.org",),
        "ACADEMIC",
        "PRIMARY",
        ("ACADEMIC",),
        "site:arxiv.org",
    ),
    "government": SourceProfile(
        "government",
        ("gov.cn",),
        "GOVERNMENT",
        "PRIMARY",
        ("CURRENT", "RISK"),
        "site:gov.cn",
    ),
    "recruitment": SourceProfile(
        "recruitment",
        (
            "zhipin.com",
            "liepin.com",
            "zhaopin.com",
            "51job.com",
            "nowcoder.com",
            "linkedin.com",
        ),
        "JOB_POSTING",
        "SECONDARY",
        ("GENERAL", "CURRENT", "COMPARISON", "RISK"),
    ),
    "csdn": SourceProfile(
        "csdn",
        ("csdn.net",),
        "COMMUNITY_TECH",
        "SECONDARY",
        ("GENERAL", "DOCUMENTATION"),
        "site:csdn.net",
    ),
    "juejin": SourceProfile(
        "juejin",
        ("juejin.cn",),
        "COMMUNITY_TECH",
        "SECONDARY",
        ("GENERAL", "DOCUMENTATION"),
        "site:juejin.cn",
    ),
    "linuxdo": SourceProfile(
        "linuxdo",
        ("linux.do",),
        "COMMUNITY_TECH",
        "SECONDARY",
        ("GENERAL", "CURRENT", "COMPARISON", "RISK"),
    ),
    "zhihu": SourceProfile(
        "zhihu",
        ("zhihu.com",),
        "COMMUNITY_EXPERIENCE",
        "SECONDARY",
        ("GENERAL", "COMPARISON"),
        "site:zhihu.com",
    ),
    "xiaohongshu": SourceProfile(
        "xiaohongshu",
        ("xiaohongshu.com", "xhslink.com"),
        "COMMUNITY_EXPERIENCE",
        "SECONDARY",
        ("GENERAL", "COMPARISON"),
        "site:xiaohongshu.com",
    ),
    "tieba": SourceProfile(
        "tieba",
        ("tieba.baidu.com",),
        "COMMUNITY_EXPERIENCE",
        "SECONDARY",
        ("GENERAL", "CURRENT", "COMPARISON", "RISK"),
        "site:tieba.baidu.com",
    ),
    "bilibili": SourceProfile(
        "bilibili",
        ("bilibili.com",),
        "COMMUNITY_EXPERIENCE",
        "SECONDARY",
        ("GENERAL", "CURRENT", "COMPARISON", "RISK"),
        "site:bilibili.com",
    ),
    "douban": SourceProfile(
        "douban",
        ("douban.com",),
        "COMMUNITY_EXPERIENCE",
        "SECONDARY",
        ("GENERAL", "CURRENT", "COMPARISON", "RISK"),
        "site:douban.com",
    ),
}

_VALID_PROFILES = set(SOURCE_PROFILES)
_SECONDARY_OR_AGGREGATOR_DOMAINS = {
    "bendibao.com",
    "wenku.baidu.com",
    "csdn.net",
    "juejin.cn",
    "zhihu.com",
    "xiaohongshu.com",
    "xhslink.com",
    "tieba.baidu.com",
    "bilibili.com",
    "douban.com",
    "zhipin.com",
    "liepin.com",
    "zhaopin.com",
    "datahub.ac.cn",
    "nowcoder.com",
    "linkedin.com",
    "linux.do",
}
_SOURCE_ENGINE_PREFERENCES: dict[str, tuple[str, ...]] = {
    "community": ("duckduckgo", "bing", "baidu", "sogou"),
    "xiaohongshu": ("baidu", "sogou", "bing"),
    "zhihu": ("sogou", "bing", "baidu", "duckduckgo"),
    "github": ("bing", "duckduckgo", "baidu"),
    "arxiv": ("bing", "duckduckgo", "exa"),
    "recruitment": ("sogou", "bing", "baidu", "duckduckgo"),
    "csdn": ("csdn", "baidu", "bing", "sogou"),
    "juejin": ("juejin", "bing", "baidu", "sogou"),
    "linuxdo": ("linuxdo", "bing", "duckduckgo", "baidu"),
}

_SOFT_COMMUNITY_PROFILES = {
    "community",
    "xiaohongshu",
    "zhihu",
    "tieba",
    "bilibili",
    "douban",
    "csdn",
    "juejin",
    "linuxdo",
}
_HARD_SOURCE_PROFILES = {"github", "arxiv", "government"}

_OFFICIAL_DOCUMENTATION_DOMAINS = {"python": "docs.python.org"}


def official_documentation_domains(query: str, *, extended: bool = False) -> tuple[str, ...]:
    """Publisher scopes verified in acceptance; unknown publishers stay unscoped."""
    folded = query.casefold()
    publishers = dict(_OFFICIAL_DOCUMENTATION_DOMAINS)
    if extended:
        # Python is also an installation target of uv. Prefer the product's
        # publisher rather than compiling "uv Python" to site:docs.python.org.
        publishers = {"sqlite": "sqlite.org", "uv": "docs.astral.sh", **publishers}
    return tuple(domain for name, domain in publishers.items()
                 if re.search(r"(?<![a-z0-9])" + re.escape(name) + r"(?![a-z0-9])", folded))


def official_reference_urls(query: str, *, extended: bool = False) -> tuple[str, ...]:
    """Known API reference bookmarks; fetching still owns evidence verification."""
    if extended:
        domains = official_documentation_domains(query, extended=True)
        if "sqlite.org" in domains and re.search(r"\bwal\b", query, re.I):
            return ("https://www.sqlite.org/wal.html",)
        if "docs.astral.sh" in domains:
            if any(word in query.casefold() for word in ("python", "版本")):
                return ("https://docs.astral.sh/uv/concepts/python-versions/",)
            if any(word in query.casefold() for word in ("install", "安装")):
                return ("https://docs.astral.sh/uv/getting-started/installation/",)
    if "docs.python.org" not in official_documentation_domains(query) or "asyncio" not in query.casefold():
        return ()
    version = re.search(r"\b3\.\d{1,2}\b", query)
    branch = version.group() if version else "3"
    return (f"https://docs.python.org/{branch}/library/asyncio-task.html",)


def source_matches_requested_version(value: str, specifications: list[str]) -> bool:
    """A moving major-version documentation branch cannot prove a pinned minor."""
    from urllib.parse import urlsplit

    requested = {v for term in specifications for v in re.findall(r"\b\d+\.\d+(?:\.\d+)?\b", term)}
    path = urlsplit(value).path
    explicit = set(re.findall(r"/(?:v)?(\d+\.\d+(?:\.\d+)?)(?:/|$)", path))
    explicit.update(re.findall(r"/(\d+)/(?:library|reference|api)(?:/|$)", path))
    return not requested or not explicit or bool(requested & explicit)


def is_search_redirect_url(value: str) -> bool:
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    host = (parsed.hostname or '').casefold().removeprefix('www.')
    return host in {'sogou.com', 'baidu.com', 'bing.com'} and (
        parsed.path.casefold() == '/link' or parsed.path.casefold().startswith('/ck/')
    )


def source_domains(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    domains: list[str] = []
    for profile_id in normalize_source_profiles(values):
        for domain in SOURCE_PROFILES[profile_id].domains:
            if domain not in domains:
                domains.append(domain)
    return tuple(domains)


def source_url_matches_profiles(
    value: str,
    profiles: list[str] | tuple[str, ...] | None,
) -> bool:
    """Check the final page URL against the requested source scope."""
    from urllib.parse import urlsplit

    host = (urlsplit(value).hostname or "").casefold().removeprefix("www.")
    normalized = set(normalize_source_profiles(profiles))
    for profile in normalized:
        profile_domains = SOURCE_PROFILES[profile].domains
        if profile_domains and any(
            host == domain or host.endswith(f".{domain}")
            for domain in profile_domains
        ):
            return True
        if profile == "official_docs" and not any(
            host == domain or host.endswith(f".{domain}")
            for domain in _SECONDARY_OR_AGGREGATOR_DOMAINS
        ):
            return True
        if profile == "community":
            return True
    return False


def is_source_entry_url(
    value: str,
    profiles: list[str] | tuple[str, ...] | None,
) -> bool:
    """Reject profile home/search pages that rarely contain task evidence."""
    from urllib.parse import urlsplit

    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().removeprefix("www.")
    path = parsed.path.casefold().rstrip("/") or "/"
    normalized = set(normalize_source_profiles(profiles))
    if "zhihu" in normalized and host == "zhihu.com" and path == "/":
        return True
    if (
        "xiaohongshu" in normalized
        and (host == "xiaohongshu.com" or host.endswith(".xiaohongshu.com"))
        and path in {"/", "/explore", "/search"}
    ):
        return True
    return False

def source_engine_preferences(values: list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    engines: list[str] = []
    for profile_id in normalize_source_profiles(values):
        for engine in _SOURCE_ENGINE_PREFERENCES.get(profile_id, ()):
            if engine not in engines:
                engines.append(engine)
    return tuple(engines)
_ENTITY_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:[A-Z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+|[A-Z][A-Za-z0-9]{2,}|[A-Za-z]+\d+[A-Za-z0-9]*)(?![A-Za-z0-9])"
)
_GENERIC_ENTITIES = {"API", "HTTP", "HTTPS", "JSON", "README", "URL", "SDK"}


def normalize_source_profiles(values: list[str] | tuple[str, ...] | None) -> list[str]:
    return list(
        dict.fromkeys(
            str(value).strip().casefold()
            for value in (values or ())
            if str(value).strip().casefold() in _VALID_PROFILES
        )
    )


def extract_anchor_entities(*texts: str) -> list[str]:
    anchors: list[str] = []
    for text in texts:
        for match in _ENTITY_PATTERN.finditer(text or ""):
            value = match.group().strip(".,:;!?()[]{}")
            if value.upper() in _GENERIC_ENTITIES or value.casefold() in {"github", "arxiv"}:
                continue
            if value not in anchors and len(value) >= 3:
                anchors.append(value)
    return anchors[:6]


def infer_source_profiles(
    query: str,
    purpose: str,
    brief: ResearchBrief | None = None,
) -> list[str]:
    requested = normalize_source_profiles(brief.source_profiles if brief else [])
    normalized = query.casefold()
    if any(marker in normalized for marker in ("github", "仓库", "开源项目", "源码", "readme")):
        requested.append("github")
    if purpose.upper() == "ACADEMIC" or any(
        marker in normalized for marker in ("论文", "学术", "arxiv", "survey")
    ):
        requested.append("arxiv")
    if purpose.upper() in {"CURRENT", "RISK"} and any(
        marker in normalized for marker in ("政策", "法规", "通知", "政府", "gov.cn")
    ):
        requested.append("government")
    if purpose.upper() == "DOCUMENTATION":
        requested.append("official_docs")
    if any(
        marker in normalized
        for marker in ("社区", "经验", "避坑", "测评", "真实反馈", "用户评价", "讨论", "帖子")
    ):
        requested.append("community")
    if any(
        marker in normalized
        for marker in (
            "招聘",
            "任职要求",
            "招聘要求",
            "实习",
            "校招",
            "社招",
            "job description",
            "job posting",
        )
    ):
        requested.append("recruitment")
    if "知乎" in normalized:
        requested.append("zhihu")
    if "小红书" in normalized:
        requested.append("xiaohongshu")
    if "贴吧" in normalized:
        requested.append("tieba")
    if "哔哩哔哩" in normalized or "b站" in normalized:
        requested.append("bilibili")
    if "豆瓣" in normalized:
        requested.append("douban")
    if any(
        marker in normalized for marker in ("python", "fastapi", "前端", "后端", "开发", "代码")
    ):
        requested.extend(("csdn", "juejin"))
    return normalize_source_profiles(requested)[:4]


def compile_source_queries(
    subquery: ResearchSubquery,
    *,
    original_question: str = "",
    brief: ResearchBrief | None = None,
) -> list[str]:
    """Compile bounded exact/entity/source queries without losing the user's anchors."""
    query = " ".join(subquery.query.split()).strip()
    anchors = list(
        dict.fromkeys([*subquery.anchor_entities, *extract_anchor_entities(query)])
    )[:4]
    explicit_profiles = normalize_source_profiles(subquery.source_profiles)
    profiles = (
        explicit_profiles
        if explicit_profiles
        else infer_source_profiles(query, subquery.purpose, brief)
    )[:4]
    terms = [term.strip() for term in subquery.required_terms if term.strip()]
    base_terms = [term for term in query.split() if term]
    for anchor in anchors:
        if anchor.casefold() not in " ".join(base_terms).casefold():
            base_terms.insert(0, anchor)
    if terms:
        base_terms.extend(
            term
            for term in terms
            if term.casefold() not in {item.casefold() for item in base_terms}
        )
    quoted = " ".join(
        f'"{anchor}"' if re.search(r"[-_\s]", anchor) else anchor for anchor in anchors
    )
    remainder = " ".join(
        item
        for item in base_terms
        if item.casefold() not in {anchor.casefold() for anchor in anchors}
    )
    # Remove one literal entity occurrence before adding quoted entities back.
    for anchor in anchors:
        remainder = remainder.replace(anchor, " ", 1)
    remainder = " ".join(remainder.split())
    if quoted and remainder and all(re.fullmatch(r"[\u4e00-\u9fff]+", item) for item in anchors):
        primary = f"{quoted}{remainder}".strip()
    else:
        primary = " ".join(item for item in (quoted, remainder) if item).strip()
    primary = primary or query
    variants: list[str] = []
    soft_community = bool(set(profiles) & _SOFT_COMMUNITY_PROFILES) and (
        "recruitment" not in profiles
    )
    query_role = (subquery.query_role or "DISCOVERY").upper()
    purpose = (subquery.purpose or "GENERAL").upper()
    if soft_community:
        if purpose in {"RISK", "COMPARISON"} or query_role in {"COMMUNITY", "VERIFY"}:
            community_cues = "实测 体验 踩坑 避坑 对比"
        else:
            community_cues = "真实经验 讨论 体验"
        community_query = f"{primary} {community_cues}".strip()
        if community_query.casefold() != query.casefold():
            variants.append(community_query[:500])
    if "recruitment" in profiles:
        recruitment_cues = "招聘 岗位 任职要求"
        recruitment_query = f"{primary} {recruitment_cues}".strip()
        if recruitment_query.casefold() != query.casefold():
            variants.append(recruitment_query[:500])
    for profile_id in profiles:
        profile = SOURCE_PROFILES[profile_id]
        if profile_id == "github" and anchors:
            repository_entity = project_name(query) or anchors[0]
            candidate = f'site:github.com "{repository_entity}" {remainder}'.strip()
        elif profile_id in _HARD_SOURCE_PROFILES and profile.query_prefix:
            candidate = f"{profile.query_prefix} {primary}".strip()
        else:
            continue
        if candidate.casefold() != query.casefold():
            variants.append(candidate)
        if len(variants) >= 2:
            break
    if not variants and primary.casefold() != query.casefold():
        variants.append(primary)
    return list(dict.fromkeys(variants))
