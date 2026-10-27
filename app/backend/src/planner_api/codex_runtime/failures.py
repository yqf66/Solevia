"""Safe Codex failures shared by transport, workers and user-facing recovery.

Never expose upstream messages: they can contain credentials, URLs and paths.
Structured error variants take precedence over message fallbacks (notably 429).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import math
import re
from urllib.error import HTTPError


_SAVED = "已保存的输入和计划进度会保留。"
# Code -> (safe explanation / next step, automatically retry a read-only turn).
_FAILURES = {
    "CODEX_QUOTA_EXHAUSTED": ("Codex 可用额度已用完。请等待额度恢复，或打开模型连接切换到自配置 API 后继续。", False),
    "CODEX_RATE_LIMITED": ("Codex 请求过于频繁，暂时受到限流。请稍后重试，或切换模型后继续。", True),
    "CODEX_MODEL_BUSY": ("Codex 所选模型当前繁忙、可用容量不足。请稍后重试，或在对话框更换模型。", True),
    "CODEX_NETWORK_ERROR": ("Codex 无法稳定连接模型服务。请检查网络、代理或服务可用状态，恢复后重试。", True),
    "CODEX_SERVICE_ERROR": ("Codex 模型服务暂时异常。请稍后重试，或打开模型连接切换到自配置 API。", True),
    "CODEX_LOGIN_REQUIRED": ("Codex 登录已失效或尚未登录。请先在 Codex 完成登录，再到模型连接点击「检测并连接」。", False),
    "CODEX_ACCESS_DENIED": ("Codex 当前没有访问权限。请检查账号、模型权限或程序拦截记录；文件权限需由你明确授权。", False),
    "CODEX_MODEL_UNAVAILABLE": ("所选模型在当前 Codex 连接中不可用，请在对话框切换模型后重试。", False),
    "CODEX_CONTEXT_TOO_LONG": ("本次内容超过 Codex 模型的上下文容量。请缩小本次调整范围、减少所选附件，或更换容量更大的模型。", False),
    "CODEX_INVALID_REQUEST": ("当前 Codex 版本或模型不接受这次请求。请更新 Codex 或更换模型后重试。", False),
    "CODEX_NOT_FOUND": ("尚未找到本机 Codex。请先安装 Codex，再到模型连接点击「检测并连接」。", False),
    "CODEX_DISCONNECTED": ("Codex 本机进程连接已中断。请到模型连接点击「检测并连接」，连接恢复后继续。", True),
    "CODEX_NOT_CONNECTED": ("Codex 尚未连接。请先在模型连接中连接本机 Codex，或配置 API 后继续。", False),
    "CODEX_TIMEOUT": ("Codex 本次生成已达到等待上限，已停止等待并尝试中断本次请求。请稍后重试，或调整模型与思考强度。", False),
    "CODEX_QUEUE_TIMEOUT": ("Codex 正在处理其他请求，本次排队等待已到上限。请等待其他请求结束后重试。", False),
    "CODEX_INTERRUPTED": ("Codex 本次生成已被中断。请确认当前进度后重新发起未完成部分。", False),
    "CODEX_SAFETY_BLOCKED": ("Codex 未能处理本次内容。请调整问题或附件范围后重试。", False),
    "CODEX_EMPTY_RESPONSE": ("Codex 本次没有返回可用内容。请稍后重试或在对话框更换模型。", True),
    "CODEX_VALIDATION_EXHAUSTED": ("Codex 返回的内容经有限修复后仍未通过校验。请重试或更换模型；系统不会放宽日期、容量和任务完整性规则。", False),
    "CODEX_OUTCOME_UNKNOWN": ("生成中断前已发起文件操作，结果需要核实。请先查看对话中的文件与确认状态，再决定是否重试。", False),
    "CODEX_GENERATION_UNAVAILABLE": ("Codex 本次生成未完成。请稍后重试；如持续出现，请在模型连接检查本机 Codex 或切换到自配置 API。", False),
}


@dataclass(frozen=True)
class CodexFailure:
    code: str
    retry_after: float | None = None

    @property
    def retryable(self):
        return _FAILURES[self.code][1]

    @property
    def message(self):
        return _FAILURES[self.code][0] + _SAVED

    def as_error(self):
        from planner_api.ai import ProviderUnavailableError
        error = ProviderUnavailableError(self.message, code=self.code, diagnostic=self.code)
        error.codex_failure = self
        # The runtime owns transport retries. Shared providers must not replay them.
        error.retryable = False
        return error


def failure_for_code(code: str | None) -> CodexFailure | None:
    return CodexFailure(code) if code in _FAILURES else None


def _retry_after(value):
    if not isinstance(value, dict):
        return None
    for key in ("retryAfterSeconds", "retry_after_seconds", "retryAfter", "retry_after"):
        try:
            seconds = float(value[key])
            if math.isfinite(seconds) and seconds >= 0:
                return seconds
        except (KeyError, TypeError, ValueError, OverflowError):
            pass
    for child in value.values():
        result = _retry_after(child)
        if result is not None:
            return result
    return None


def classify_failure(error) -> CodexFailure:
    known = getattr(error, "codex_failure", None)
    if known:
        return known
    code = getattr(error, "code", None)
    if code in _FAILURES:
        return CodexFailure(code)
    detail = getattr(error, "details", error)
    if not isinstance(detail, (dict, str)):
        detail = str(detail)
    text = json.dumps(detail, ensure_ascii=False) if isinstance(detail, dict) else detail
    compact = re.sub(r"[^a-z0-9]", "", text.lower())
    status = error.code if isinstance(error, HTTPError) else None
    found = re.search(r'(?:httpStatusCode|status_code|status|HTTP|status code)[\s\"\':=]*(\d{3})', text, re.I)
    if found:
        status = int(found[1])
    retry_after = _retry_after(detail)
    if isinstance(error, HTTPError) and error.headers:
        try:
            retry_after = float(error.headers.get("Retry-After", ""))
        except (TypeError, ValueError):
            pass
    def result(name):
        return CodexFailure(name, retry_after=retry_after)
    if any(word in compact for word in ("usagelimit", "quota", "creditbalance", "insufficientbalance", "notenoughcredits", "billinghardlimit")) or status == 402 or "额度" in text:
        return result("CODEX_QUOTA_EXHAUSTED")
    if any(word in compact for word in ("contextwindowexceeded", "contextlengthexceeded", "maximumcontextlength", "contexttoolong", "toomanytokens")):
        return result("CODEX_CONTEXT_TOO_LONG")
    if any(word in compact for word in ("unauthorized", "authentication", "tokenexpired", "invalidapikey", "loginrequired")) or status == 401 or "未登录" in text:
        return result("CODEX_LOGIN_REQUIRED")
    if re.search(r'model.{0,250}(?:not available|not found|not supported|does not exist)', text, re.I) or "modelnotfound" in compact:
        return result("CODEX_MODEL_UNAVAILABLE")
    if any(word in compact for word in ("overloaded", "overcapacity", "atcapacity", "capacityexceeded", "modelcapacity", "maximumcapacity", "serverbusy")) or "容量不足" in text:
        return result("CODEX_MODEL_BUSY")
    if status == 429 or any(word in compact for word in ("ratelimit", "toomanyrequests")):
        return result("CODEX_RATE_LIMITED")
    if status == 408:
        return result("CODEX_NETWORK_ERROR")
    if status == 409:
        return result("CODEX_MODEL_BUSY")
    if status == 403 or any(word in compact for word in ("permissiondenied", "accessdenied", "forbidden", "sandboxerror")) or isinstance(error, PermissionError):
        return result("CODEX_ACCESS_DENIED")
    if any(word in compact for word in ("contentfilter", "safetyblocked")):
        return result("CODEX_SAFETY_BLOCKED")
    if status == 400 or any(word in compact for word in ("badrequest", "invalidrequest", "invalidparams", "invalidjsonschema", "methodnotfound", "notsupported")):
        return result("CODEX_INVALID_REQUEST")
    if isinstance(error, FileNotFoundError):
        return result("CODEX_NOT_FOUND")
    if (status and status >= 500) or "internalservererror" in compact:
        return result("CODEX_SERVICE_ERROR")
    if isinstance(error, BrokenPipeError) or any(word in compact for word in ("appserver未运行", "appserver连接已关闭", "appserver已停止")) or "连接已关闭" in text:
        return result("CODEX_DISCONNECTED")
    if isinstance(error, TimeoutError):
        return result("CODEX_TIMEOUT")
    if any(word in compact for word in ("responseconnectionfailed", "responseconnectiondisconnected", "responsestreamconnectionfailed", "responsestreamdisconnected", "responsetoomanyfailedattempts", "httpconnectionfailed", "connectionreset", "connectionrefused", "dns", "network", "tls", "certificate", "temporarydisconnection", "timedout", "errorsendingrequest", "failedtoconnect", "getaddrinfofailed", "streamfinishedunexpectedly", "connectionclosedbeforemessagecompleted")):
        return result("CODEX_NETWORK_ERROR")
    if "interrupted" in compact or "cancelled" in compact:
        return result("CODEX_INTERRUPTED")
    return result("CODEX_GENERATION_UNAVAILABLE")


def retry_delay(failure: CodexFailure, attempt: int, remaining: float, *, jitter: float = 0) -> float | None:
    """Honor an upstream cooldown in full; never shorten it into a request storm."""
    delay = max(2 ** (attempt + 1) + jitter, failure.retry_after or 0)
    return delay if math.isfinite(delay) and delay + 0.1 < remaining else None
