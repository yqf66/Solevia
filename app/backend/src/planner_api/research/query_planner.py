from __future__ import annotations

import hashlib
import re

from .schemas import ResearchQueryConstraints, ResearchSubquery

_PURPOSES = {"GENERAL", "ACADEMIC", "CURRENT", "DOCUMENTATION", "COMPARISON", "RISK"}
_PRIMARY_PROFILES = {"official_docs", "government", "github", "arxiv"}
_COMMUNITY_PROFILES = {
    "community",
    "zhihu",
    "xiaohongshu",
    "tieba",
    "bilibili",
    "douban",
    "csdn",
    "juejin",
}
_QUESTION_PREFIX = re.compile(
    r"^(?:请(?:帮我)?(?:查证|查询|搜索|检索|确认|看看)|请问|想知道|我想知道|"
    r"请(?:联网)?(?:调查|研究|了解)|联网(?:调查|研究|了解)|"
    r"如何|怎么|为什么|是否|能否|有没有)\s*[:：,，]?\s*",
    re.IGNORECASE,
)
_RETRIEVAL_PREFIX = re.compile(
    r"^(?:请(?:帮我)?(?:查证|查询|搜索|检索|确认|看看)|检索|查找|查证|核对|收集)\s*",
    re.IGNORECASE,
)
_SOURCE_RETRIEVAL_PREFIX = re.compile(
    r"^(?:在)?(?P<source>.{1,48}?)中(?:检索|查找|查证|核对|收集)\s*"
)
_QUESTION_WORDS = re.compile(r"(?:是否|能否|有没有|怎么|如何|为什么|请问)")
_QUERY_PUNCTUATION = re.compile(r"[?？!！,，、。;；:：()（）\[\]【】{}‘’“”]+")
_ASCII_TERM = re.compile(r"[A-Za-z][A-Za-z0-9_-]{2,}")
_CHINESE_TEXT = re.compile(r"[\u4e00-\u9fff]+")
_QUOTED_ENTITY = re.compile(r"[「『“\"]([^」』”\"]{2,48})[」』”\"]|`([^`]{2,48})`")
_KNOWN_CJK_ENTITIES = (
    "小红书",
    "知乎",
    "贴吧",
    "哔哩哔哩",
    "豆瓣",
    "抖音",
    "微信公众号",
)
_GENERIC_ENTITY_WORDS = {
    "官方",
    "文档",
    "资料",
    "社区",
    "经验",
    "问题",
    "方法",
    "实践",
    "搜索",
    "联网",
}
_PROJECT_NAME = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9]*(?:[-_][A-Za-z0-9]+)+"
    r"|(?<![A-Za-z0-9])[A-Za-z]+\d+[A-Za-z0-9]*"
)
_HARDWARE_MODEL = re.compile(
    r"(?<![A-Za-z0-9])(?:RTX|GTX|RX)\s*\d{3,4}(?:\s*(?:Ti|SUPER|XT|XTX))?(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_SPECIFICATION_TERM = re.compile(
    r"(?<![A-Za-z0-9])\d+(?:\.\d+)?\s*(?:"
    r"GB|GiB|MB|MiB|TB|TiB|ms|s|tokens?/s|%|元|万元|小时|分钟|天|晚|周|个月|年|人|公里|km"
    r")(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_VERSION_TERM = re.compile(r"(?<![A-Za-z0-9])v?\d+\.\d+(?:\.\d+)?(?![A-Za-z0-9])", re.IGNORECASE)
_YEAR_TERM = re.compile(r"(?<!\d)20\d{2}(?!\d)")
_PLATFORM_TERMS = ("Windows", "Linux", "macOS", "Android", "iOS", "WSL2")
_METRIC_TERMS = (
    "显存",
    "内存",
    "磁盘",
    "速度",
    "吞吐",
    "延迟",
    "OOM",
    "兼容性",
    "资源占用",
    "功耗",
    "价格",
    "成本",
    "预算",
    "薪资",
    "时长",
    "耗时",
    "距离",
    "通勤时间",
    "成功率",
    "通过率",
    "评分",
    "覆盖率",
    "新鲜度",
    "发布日期",
    "续航",
    "岗位数量",
)
_RELATION_MARKERS = (
    "同时运行",
    "并行运行",
    "依赖",
    "对比",
    "比较",
    "影响",
    "适用于",
    "通过",
    "结合",
)


def _constraint_key(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.casefold())


def _unique_terms(values: list[str], limit: int) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = " ".join(value.split()).strip()
        key = _constraint_key(cleaned)
        if not cleaned or not key or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


def _normalize_specification(value: str) -> str:
    compact = re.sub(r"\s+", "", value)
    year = re.fullmatch(r"(20\d{2})年?", compact)
    return year.group(1) if year else compact


def _unique_entities(values: list[str], limit: int) -> list[str]:
    unique = _unique_terms(values, limit * 2)
    result: list[str] = []
    for value in unique:
        key = _constraint_key(value)
        if any(
            key != _constraint_key(existing)
            and key in _constraint_key(existing)
            and len(_constraint_key(existing)) > len(key)
            for existing in result
        ):
            continue
        result.append(value)
        if len(result) >= limit:
            break
    return result


def repository_fallback_query(query: str) -> str | None:
    """Try an exact project name when ordinary search has no usable candidates."""
    name = project_name(query)
    if name is None:
        return None
    return f'site:github.com "{name}"'


def project_name(query: str) -> str | None:
    match = max(_PROJECT_NAME.finditer(query), key=lambda item: len(item.group()), default=None)
    return match.group() if match else None


def align_question_aspects(
    questions: list[ResearchSubquery], required_aspects: list[str]
) -> None:
    """Canonicalize harmless punctuation differences without guessing semantics."""
    required = {re.sub(r"\W+", "", aspect.casefold()): aspect for aspect in required_aspects}
    for question in questions:
        question.covers_aspects = list(
            dict.fromkeys(
                required.get(re.sub(r"\W+", "", aspect.casefold()), aspect)
                for aspect in question.covers_aspects
            )
        )
        if question.primary_aspect:
            question.primary_aspect = required.get(
                re.sub(r"\W+", "", question.primary_aspect.casefold()),
                question.primary_aspect,
            )
        if question.primary_aspect not in question.covers_aspects:
            question.primary_aspect = (
                question.covers_aspects[0] if question.covers_aspects else ""
            )


def compact_search_query(value: str) -> str:
    """Convert a conversational question into a restrained search phrase."""
    original = " ".join(value.split()).strip()
    compacted = _QUESTION_PREFIX.sub("", original)
    source_match = _SOURCE_RETRIEVAL_PREFIX.match(compacted)
    if source_match:
        compacted = f"{source_match.group('source')} {compacted[source_match.end():]}"
    compacted = _RETRIEVAL_PREFIX.sub("", compacted)
    compacted = re.sub(r"(?:官方仓库|官方文档)中关于(?:其|该项目|该工具)?", "官方 ", compacted)
    compacted = re.sub(r"中关于", " ", compacted)
    compacted = re.sub(r"(?:的)?(?:具体)?(?:说明与限制|真实使用经验与分工建议)", " ", compacted)
    compacted = _QUESTION_WORDS.sub(" ", compacted)
    compacted = _QUERY_PUNCTUATION.sub(" ", compacted)
    compacted = re.sub(r"(?:以及其|及其)", " ", compacted)
    compacted = re.sub(r"(?:的)?(?:具体)?说明", " ", compacted)
    compacted = re.sub(
        r"(?:并结合|结合).{0,80}(?:给出|提出|形成).{0,80}$",
        " ",
        compacted,
    )
    compacted = re.sub(
        r"(?:给出|提出|形成)(?:一个|具体)?(?:可行|合理)?(?:配置|方案|建议|结论).*$",
        " ",
        compacted,
    )
    compacted = " ".join(compacted.split()).strip(" .。")
    return compacted if len(compacted) >= 2 else original


def infer_query_role(
    purpose: str,
    source_profiles: list[str],
    query_intent: str = "",
) -> str:
    """Infer the query's evidence role without hard-coding one research template."""
    profiles = {item.casefold() for item in source_profiles}
    normalized = query_intent.casefold()
    purpose = purpose.upper()
    if purpose == "RISK" or any(
        marker in normalized
        for marker in ("核对", "验证", "冲突", "release", "版本号", "发布时间")
    ):
        return "VERIFY"
    has_primary = bool(profiles & _PRIMARY_PROFILES)
    has_community = bool(profiles & _COMMUNITY_PROFILES)
    if any(
        marker in normalized
        for marker in ("社区", "经验", "踩坑", "讨论", "反馈", "口碑")
    ):
        return "COMMUNITY"
    if purpose == "DOCUMENTATION" and has_primary:
        return "AUTHORITATIVE"
    if has_primary and not has_community:
        return "AUTHORITATIVE"
    if has_community and not has_primary:
        return "COMMUNITY"
    return "DISCOVERY"


def classify_query_purpose(query: str) -> str:
    normalized = query.casefold()
    ascii_tokens = set(re.findall(r"[a-z0-9]+", normalized))
    groups = (
        ("ACADEMIC", ("论文", "学术", "期刊", "研究报告", "paper", "journal", "arxiv")),
        ("CURRENT", ("最新", "近期", "现状", "current", "latest", "recent", "202")),
        (
            "DOCUMENTATION",
            (
                "官方", "文档", "安装", "配置", "api", "docs", "install",
                "official", "documentation", "repository",
            ),
        ),
        ("COMPARISON", ("对比", "区别", "比较", "versus", "vs", "compare")),
        ("RISK", ("风险", "安全", "限制", "漏洞", "risk", "security", "limitation")),
    )
    for purpose, markers in groups:
        if any(
            marker in ascii_tokens if marker.isascii() else marker in normalized
            for marker in markers
        ):
            return purpose
    return "GENERAL"


def normalize_research_questions(
    questions: list[str | ResearchSubquery], *, max_subqueries: int = 12
) -> list[ResearchSubquery]:
    """Normalize model-proposed questions while retaining planning metadata."""
    normalized: list[ResearchSubquery] = []
    seen: set[str] = set()
    for value in questions:
        if isinstance(value, ResearchSubquery):
            candidate = value.model_copy(deep=True)
            original_query = " ".join(candidate.query.split()).strip()
        else:
            original_query = " ".join(value.split()).strip()
            candidate = ResearchSubquery(
                query=original_query,
                purpose=classify_query_purpose(original_query),
            )
        query = compact_search_query(original_query)
        normalized_query = query.casefold()
        if not query or normalized_query in seen:
            continue
        seen.add(normalized_query)
        candidate.query = query
        if candidate.purpose.upper() not in _PURPOSES:
            candidate.purpose = classify_query_purpose(query)
        else:
            candidate.purpose = candidate.purpose.upper()
        candidate.query_intent = (
            " ".join(candidate.query_intent.split()).strip() or original_query
        )
        candidate.query_role = (
            infer_query_role(
                candidate.purpose,
                candidate.source_profiles,
                candidate.query_intent or original_query,
            )
            if candidate.query_role == "DISCOVERY"
            else candidate.query_role
        )
        candidate.primary_aspect = (
            candidate.primary_aspect.strip()
            or (candidate.covers_aspects[0] if candidate.covers_aspects else "")
        )
        synchronize_query_constraints(candidate)
        candidate.query = preserve_query_constraints(candidate)
        normalized_query = candidate.query.casefold()
        if not candidate.key:
            candidate.key = "q_" + hashlib.sha1(normalized_query.encode("utf-8")).hexdigest()[:12]
        candidate.status = "PLANNED"
        normalized.append(candidate)
        if len(normalized) >= max_subqueries:
            break
    return normalized


def extract_query_entities(value: str) -> list[str]:
    """Extract small, stable entity hints without attempting full NLP."""
    text = value or ""
    entities: list[str] = []
    for match in _HARDWARE_MODEL.finditer(text):
        value = " ".join(match.group().split()).upper().replace(" TI", " Ti")
        if value not in entities:
            entities.append(value)
    for match in _QUOTED_ENTITY.finditer(text):
        value = (match.group(1) or match.group(2) or "").strip()
        if value and value not in _GENERIC_ENTITY_WORDS and value not in entities:
            entities.append(value)
    for match in _PROJECT_NAME.finditer(text):
        value = match.group().strip()
        if value not in entities:
            entities.append(value)
    for match in re.finditer(r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9+#.-]{2,30}", text):
        value = match.group().strip(".,:;!?()[]{}")
        if any(
            entity.casefold().startswith(f"{value.casefold()} ")
            for entity in entities
        ):
            continue
        if value.casefold() not in {item.casefold() for item in _GENERIC_ENTITY_WORDS}:
            if value not in entities:
                entities.append(value)
    for entity in _KNOWN_CJK_ENTITIES:
        if entity in text and entity not in entities:
            entities.append(entity)
    return entities[:6]


def extract_query_required_terms(value: str) -> list[str]:
    """Return legacy required terms derived from the structured contract."""
    contract = extract_query_constraints(value)
    return list(dict.fromkeys([*contract.specifications, *contract.metrics]))[:8]


def extract_query_constraints(
    *values: str,
    seed: ResearchQueryConstraints | None = None,
) -> ResearchQueryConstraints:
    """Build a domain-neutral constraint contract from query text and model hints."""
    result = seed.model_copy(deep=True) if seed is not None else ResearchQueryConstraints()
    text = " ".join(value for value in values if value)
    result.entities = _unique_entities(
        [*result.entities, *extract_query_entities(text)],
        8,
    )
    specifications = list(result.specifications)
    for pattern in (_SPECIFICATION_TERM, _VERSION_TERM, _YEAR_TERM):
        for match in pattern.finditer(text):
            specifications.append(_normalize_specification(match.group()))
    folded = text.casefold()
    for platform in _PLATFORM_TERMS:
        if platform.casefold() in folded and platform not in specifications:
            specifications.append(platform)
    result.specifications = _unique_terms(specifications, 8)
    result.metrics = _unique_terms(
        [
            *result.metrics,
            *(term for term in _METRIC_TERMS if term.casefold() in folded),
        ],
        8,
    )
    result.relations = _unique_terms(
        [
            *result.relations,
            *(term for term in _RELATION_MARKERS if term.casefold() in folded),
        ],
        8,
    )
    if "先" in text and "再" in text and "先...再..." not in result.relations:
        result.relations.append("先...再...")
    if re.search(r"从.{1,24}到", text) and "从...到..." not in result.relations:
        result.relations.append("从...到...")
    result.relations = result.relations[:8]
    result.relaxable_terms = _unique_terms(
        [*result.relaxable_terms, *result.relations, *result.metrics],
        12,
    )
    return result


def merge_query_constraints(
    *contracts: ResearchQueryConstraints,
) -> ResearchQueryConstraints:
    """Merge compatible contracts without assigning domain-specific precedence."""
    merged = ResearchQueryConstraints()
    for field_name, limit in (
        ("entities", 8),
        ("specifications", 8),
        ("relations", 8),
        ("metrics", 8),
        ("strict_terms", 12),
        ("literal_terms", 12),
        ("relaxable_terms", 12),
    ):
        values = [value for contract in contracts for value in getattr(contract, field_name)]
        if field_name == "entities":
            setattr(merged, field_name, _unique_entities(values, limit))
        else:
            setattr(merged, field_name, _unique_terms(values, limit))
    return merged


def synchronize_query_constraints(
    question: ResearchSubquery,
    inherited: list[ResearchQueryConstraints] | None = None,
) -> None:
    """Synchronize the structured contract with legacy query metadata."""
    legacy = ResearchQueryConstraints(
        entities=question.anchor_entities,
    )
    detected = extract_query_constraints(
        question.query_intent,
        question.query,
        seed=question.constraints,
    )
    question.constraints = merge_query_constraints(
        *(inherited or []),
        legacy,
        detected,
    )
    question.anchor_entities = question.constraints.entities[:6]
    question.required_terms = _unique_terms(
        [
            *question.required_terms,
            *question.constraints.specifications,
            *question.constraints.metrics,
        ],
        8,
    )


def preserve_query_constraints(question: ResearchSubquery) -> str:
    """Keep model-provided entities/required terms in the executable query."""
    query = compact_search_query(question.query)
    intent = compact_search_query(question.query_intent or question.query)
    informative = [item for item in re.split(r"\s+", query) if len(item.strip()) >= 2]
    if len(informative) < 2 and len(intent) > len(query):
        query = intent
    existing = _constraint_key(query)
    required = [
        *question.constraints.literal_terms,
        *question.constraints.strict_terms,
        *question.constraints.entities,
        *question.constraints.specifications,
        *question.constraints.metrics,
        *question.anchor_entities,
        *question.required_terms,
    ]
    def already_present(term: str) -> bool:
        if _constraint_key(term) in existing:
            return True
        # Model constraints often paraphrase a phrase already present in the query.
        cleaned = re.sub(r"版本(?:文档)?|\bversion\b", " ", term, flags=re.IGNORECASE)
        def normalize(text: str) -> str:
            return re.sub(r"\b(?:docs?|documentation)\b", "documentation", text.casefold())
        tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalize(cleaned)))
        query_tokens = set(re.findall(r"[a-z0-9]+|[\u4e00-\u9fff]+", normalize(query)))
        return bool(tokens) and tokens <= query_tokens

    missing = _unique_terms([
        item.strip()
        for item in required
        if item.strip() and not already_present(item)
    ], 12)
    if missing:
        query = " ".join([query, *missing[:6]]).strip()
    return " ".join(query.split())[:500]


def infer_primary_aspect(value: str, required_aspects: list[str]) -> str:
    """Match a legacy follow-up to one required aspect using bounded lexical overlap."""
    if len(required_aspects) == 1:
        return required_aspects[0]
    ascii_terms = {item.casefold() for item in _ASCII_TERM.findall(value)}
    chinese_bigrams = {
        text[index : index + 2]
        for text in _CHINESE_TEXT.findall(value)
        for index in range(max(len(text) - 1, 0))
    }
    scored: list[tuple[int, str]] = []
    for aspect in required_aspects:
        aspect_ascii = {item.casefold() for item in _ASCII_TERM.findall(aspect)}
        aspect_bigrams = {
            text[index : index + 2]
            for text in _CHINESE_TEXT.findall(aspect)
            for index in range(max(len(text) - 1, 0))
        }
        score = 3 * len(ascii_terms & aspect_ascii) + len(chinese_bigrams & aspect_bigrams)
        scored.append((score, aspect))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < 2:
        return ""
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return ""
    return scored[0][1]


def finalize_query_metadata(
    questions: list[ResearchSubquery],
    required_aspects: list[str] | None = None,
) -> None:
    """Fill query metadata after source profiles and aspect alignment are known."""
    if required_aspects:
        align_question_aspects(questions, required_aspects)
    for question in questions:
        question.query = compact_search_query(question.query)
        question.query_intent = (
            " ".join(question.query_intent.split()).strip() or question.query
        )
        synchronize_query_constraints(question)
        question.query = preserve_query_constraints(question)
        inferred_aspect = infer_primary_aspect(
            question.query_intent or question.query,
            required_aspects or [],
        )
        question.primary_aspect = (
            question.primary_aspect
            if question.primary_aspect in question.covers_aspects
            else (
                question.covers_aspects[0]
                if question.covers_aspects
                else inferred_aspect
            )
        )
        if question.primary_aspect and not question.covers_aspects:
            question.covers_aspects = [question.primary_aspect]
        if question.query_role == "DISCOVERY":
            question.query_role = infer_query_role(
                question.purpose,
                question.source_profiles,
                question.query_intent,
            )


def validate_question_dependencies(questions: list[ResearchSubquery]) -> list[str]:
    """Mark malformed dependency nodes blocked and return human-readable diagnostics."""
    by_key: dict[str, ResearchSubquery] = {}
    diagnostics: list[str] = []
    for item in questions:
        if item.key in by_key:
            item.status = "BLOCKED"
            by_key[item.key].status = "BLOCKED"
            diagnostics.append(f"研究问题 key 重复：{item.key}。")
            continue
        by_key[item.key] = item
    for item in questions:
        if item.status == "BLOCKED":
            continue
        missing = [key for key in item.depends_on if key not in by_key]
        if missing:
            item.status = "BLOCKED"
            diagnostics.append(
                f"研究问题“{item.query}”引用了不存在的依赖：{', '.join(missing)}。"
            )

    visiting: set[str] = set()
    visited: set[str] = set()
    cyclic: set[str] = set()

    def visit(key: str, stack: list[str]) -> None:
        if key in visiting:
            cyclic.update(stack[stack.index(key) :])
            return
        if key in visited:
            return
        visiting.add(key)
        stack.append(key)
        item = by_key[key]
        for dependency in item.depends_on:
            if dependency in by_key:
                visit(dependency, stack)
        stack.pop()
        visiting.remove(key)
        visited.add(key)

    for key in by_key:
        visit(key, [])
    for key in cyclic:
        by_key[key].status = "BLOCKED"
    if cyclic:
        diagnostics.append("研究问题依赖关系存在循环，循环中的问题已阻止执行。")
    return diagnostics
