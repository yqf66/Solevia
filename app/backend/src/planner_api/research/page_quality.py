from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from .schemas import PageDocument

_ERROR_PHRASES = (
    "page not found",
    "not found",
    "access denied",
    "forbidden",
    "service unavailable",
    "页面不存在",
    "网页不存在",
    "请求被拒绝",
    "服务不可用",
)
_AUTH_PHRASES = (
    "sign in to continue",
    "log in to continue",
    "login required",
    "请先登录",
    "请登录后",
    "登录后查看",
    "登录查看更多",
    "登录后继续",
    "手机号登录",
    "获取验证码",
    "注册后继续",
)
_CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "checking your browser",
    "security check",
    "验证您是人机",
    "安全验证",
    "访问验证",
    "访问异常",
    "请求过于频繁",
    "验证身份",
    "请完成验证",
    "请求存在异常",
    "暂时限制本次访问",
)
_APP_WALL_PHRASES = (
    "打开app",
    "打开 app",
    "下载app",
    "前往app",
    "在app内打开",
    "app内查看",
)
_SCRIPT_SHELL_PHRASES = (
    "请开启javascript",
    "请启用javascript",
    "enable javascript",
    "javascript is required",
)
_NAVIGATION_MARKERS = (
    "首页",
    "登录",
    "注册",
    "下载",
    "客户端",
    "打开app",
    "关注",
    "消息",
    "搜索",
)
_UNSUPPORTED_CONTENT_TYPES = ("image/", "audio/", "video/", "application/octet-stream")


@dataclass(frozen=True)
class PageQualityAssessment:
    score: float
    flags: tuple[str, ...]
    content_length: int
    usable: bool


def assess_page_quality(
    document: PageDocument,
    *,
    min_content_chars: int = 240,
) -> PageQualityAssessment:
    """Apply a cheap, deterministic quality gate before evidence enters the packet.

    The provider is allowed to attach its own flags, but the Agent owns the final
    policy so different providers behave consistently.
    """

    content = re.sub(r"\s+", " ", document.content or "").strip()
    content_lower = content.casefold()
    title_lower = (document.title or "").casefold()
    content_length = len(content)
    flags: list[str] = list(dict.fromkeys(str(flag) for flag in document.quality_flags))

    if content_length < min_content_chars:
        flags.append("LOW_TEXT")
    error_titles = {
        "403",
        "403 forbidden",
        "404",
        "404 not found",
        "500",
        "500 internal server error",
        "502 bad gateway",
        "503 service unavailable",
        "page not found",
        "not found",
        "access denied",
        "forbidden",
        "service unavailable",
        "页面不存在",
        "网页不存在",
    }
    error_title = title_lower.strip() in error_titles
    error_content = content_length < 800 and any(
        marker in content_lower[:500] for marker in _ERROR_PHRASES
    )
    if error_title or error_content:
        flags.append("ERROR_PAGE")
    auth_title = title_lower.strip() in {"login", "sign in", "登录", "注册"}
    final_path = urlsplit(str(document.final_url)).path.casefold()
    auth_redirect = any(
        marker in final_path.split("/") for marker in ("login", "signin", "passport")
    )
    if content_length < 1600 and (
        auth_title
        or auth_redirect
        or any(marker in content_lower[:1200] for marker in _AUTH_PHRASES)
    ):
        flags.append("AUTH_REQUIRED")
    if content_length < 1600 and any(
        marker in title_lower or marker in content_lower[:1200] for marker in _CHALLENGE_MARKERS
    ):
        flags.append("BOT_CHALLENGE")
    if content_length < 2000 and any(
        marker in content_lower[:1600] for marker in _APP_WALL_PHRASES
    ):
        flags.append("APP_WALL")
    if content_length < 2000 and any(
        marker in content_lower[:1600] for marker in _SCRIPT_SHELL_PHRASES
    ):
        flags.append("SCRIPT_SHELL")
    navigation_hits = sum(marker in content_lower[:1600] for marker in _NAVIGATION_MARKERS)
    navigation_only = (
        (content_length < 700 and navigation_hits >= 4)
        or (content_length < 1200 and navigation_hits >= 7)
    )
    if not document.readability_applied and navigation_only:
        flags.append("NAVIGATION_SHELL")
    content_type = (document.content_type or "").casefold()
    if any(content_type.startswith(prefix) for prefix in _UNSUPPORTED_CONTENT_TYPES):
        flags.append("UNSUPPORTED_CONTENT_TYPE")

    flags = list(dict.fromkeys(flags))
    score = 100.0
    score -= 60.0 * int("ERROR_PAGE" in flags)
    score -= 55.0 * int("BOT_CHALLENGE" in flags)
    score -= 45.0 * int("AUTH_REQUIRED" in flags)
    score -= 50.0 * int("APP_WALL" in flags)
    score -= 50.0 * int("SCRIPT_SHELL" in flags)
    score -= 45.0 * int("NAVIGATION_SHELL" in flags)
    score -= 35.0 * int("LOW_TEXT" in flags)
    score -= 35.0 * int("UNSUPPORTED_CONTENT_TYPE" in flags)
    score -= 8.0 * int(not document.readability_applied)
    score = max(0.0, min(100.0, score))

    hard_failures = {
        "ERROR_PAGE",
        "AUTH_REQUIRED",
        "BOT_CHALLENGE",
        "APP_WALL",
        "SCRIPT_SHELL",
        "NAVIGATION_SHELL",
        "UNSUPPORTED_CONTENT_TYPE",
    }
    usable = content_length >= min_content_chars and not hard_failures.intersection(flags)
    return PageQualityAssessment(
        score=score,
        flags=tuple(flags),
        content_length=content_length,
        usable=usable,
    )
