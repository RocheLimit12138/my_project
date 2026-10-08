"""One synchronous, injectable DeepSeek Chat API client for all project agents."""
from __future__ import annotations

import json
import logging
from typing import Any, Protocol, TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.llm_config import DeepSeekError, DeepSeekSettings

T = TypeVar("T", bound=BaseModel)

LOGGER = logging.getLogger(__name__)


class JsonModel(Protocol):
    """Agent-facing boundary; tests inject deterministic implementations."""
    def generate(self, *, task: str, instructions: str, payload: dict[str, Any], schema: type[T]) -> T: ...


def _unique_object(pairs):
    """Duplicate JSON keys are ambiguous, even when json.loads accepts them."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _unwrap_json_code_fence(content: str) -> str:
    """Accept a complete Markdown JSON fence without repairing model content."""
    lines = content.strip().splitlines()
    if len(lines) >= 3 and lines[0].strip().startswith(chr(96) * 3) and lines[-1].strip() == chr(96) * 3:
        return "\n".join(lines[1:-1]).strip()
    return content.strip()

class DeepSeekClient:
    """No automatic POST retries or silent offline fallback after model failure.

    Each request owns and closes its HTTP connection pool. Only sanitized errors
    escape; response bodies, prompts, API keys and reasoning_content are not logged.
    """

    def __init__(self, settings: DeepSeekSettings, *, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self._transport = transport

    def generate(self, *, task: str, instructions: str, payload: dict[str, Any], schema: type[T]) -> T:
        system = (
            instructions + "\n只输出一个 JSON 对象。严格遵循以下 JSON Schema；不要输出代码围栏。\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        )
        user = json.dumps({"task": task, "data": payload}, ensure_ascii=False, default=str, allow_nan=False)
        if len(system) + len(user) > self.settings.max_input_chars:
            LOGGER.warning(
                "DeepSeek request rejected: task=%s reason=input_too_long",
                task,
            )
            raise DeepSeekError("模型输入过长，请减少候选数或案例分页大小后重试。")
        body = {
            "model": self.settings.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},

            "stream": False,
        }
        # Proposal contracts contain long structured outputs; reduce sampling variance.
        # Other agents retain their existing sampling behavior.
        if task in {"proposal_agent", "proposal_source_repair", "proposal_grounding_review"}:
            body["temperature"] = 0
        timeout = httpx.Timeout(self.settings.timeout_seconds, connect=10, write=30, pool=10)
        try:
            with httpx.Client(timeout=timeout, transport=self._transport, follow_redirects=False) as client:
                response = client.post(
                    self.settings.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.api_key},
                    json=body,
                )
        except httpx.TimeoutException:
            LOGGER.warning("DeepSeek request failed: task=%s error_type=TimeoutException", task)
            raise DeepSeekError("DeepSeek 请求超时，请稍后重试。") from None
        except httpx.HTTPError:
            LOGGER.warning("DeepSeek request failed: task=%s error_type=HTTPError", task)
            raise DeepSeekError("DeepSeek 网络连接失败，请检查服务地址与网络。") from None
        if response.status_code != 200:
            LOGGER.warning(
                "DeepSeek request failed: task=%s status_code=%s",
                task,
                response.status_code,
            )
            message = {
                401: "DeepSeek 鉴权失败，请检查本机 API Key。",
                402: "DeepSeek 账户余额不足。",
                403: "DeepSeek 请求被拒绝，请检查账户权限。",
                429: "DeepSeek 请求限流，请稍后重试。",
            }.get(response.status_code, "DeepSeek 服务请求失败，请检查模型配置或稍后重试。")
            raise DeepSeekError(message)
        try:
            envelope = response.json()
            choice = envelope["choices"][0]
            if choice.get("finish_reason") != "stop":
                raise ValueError("incomplete completion")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError("empty content")
            parsed = json.loads(_unwrap_json_code_fence(content), object_pairs_hook=_unique_object,
                                parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
            return schema.model_validate(parsed)
        except (ValueError, TypeError, KeyError, IndexError, AttributeError, ValidationError) as exc:
            response_shape = {
                "content_len": len(content) if isinstance(content, str) else None,
                "finish_reason": choice.get("finish_reason") if isinstance(choice, dict) else None,
                "starts_with": content.lstrip()[:1] if isinstance(content, str) else None,
                "ends_with": content.rstrip()[-1:] if isinstance(content, str) else None,
                "has_json_fence": _unwrap_json_code_fence(content) != content.strip() if isinstance(content, str) else False,
                "json_error_pos": getattr(exc, "pos", None),
                "validation_issues": [
                    {"loc": ".".join(str(item) for item in issue.get("loc", ())), "type": issue.get("type")}
                    for issue in exc.errors()
                ] if isinstance(exc, ValidationError) else None,
            }
            LOGGER.warning(
                "DeepSeek response validation failed: task=%s error_type=%s shape=%s",
                task,
                type(exc).__name__,
                response_shape,
            )
            raise DeepSeekError("DeepSeek 返回的内容不完整或格式不合法，请重试。") from None


def create_deepseek_client() -> DeepSeekClient | None:
    """No request is sent during configuration or client construction."""
    settings = DeepSeekSettings.from_environment()
    return DeepSeekClient(settings) if settings is not None else None
