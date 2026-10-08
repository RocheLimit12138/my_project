"""Local TOML plus environment overrides for DeepSeek; secrets never appear in repr."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import os
from pathlib import Path
import tomllib
from urllib.parse import urlsplit

LOCAL_CONFIG_PATH = Path(__file__).resolve().parents[1] / "deepseek.local.toml"
_CONFIG_FIELDS = {
    "enabled": ("DEEPSEEK_ENABLED", False, bool),
    "api_key": ("DEEPSEEK_API_KEY", "", str),
    "base_url": ("DEEPSEEK_BASE_URL", "https://api.deepseek.com", str),
    "model": ("DEEPSEEK_MODEL", "deepseek-flash", str),
    "timeout_seconds": ("DEEPSEEK_TIMEOUT_SECONDS", 90.0, float),
    "max_tokens": ("DEEPSEEK_MAX_TOKENS", 8192, int),
    "max_input_chars": ("DEEPSEEK_MAX_INPUT_CHARS", 120000, int),
}


class DeepSeekError(RuntimeError):
    """Public, sanitized model failure; never attach provider bodies or credentials."""


class DeepSeekConfigurationError(DeepSeekError):
    """Enabled model configuration is missing or invalid."""


def _enabled_env(value: str) -> bool:
    cleaned = value.strip().lower()
    if cleaned not in {"true", "1", "false", "0"}:
        raise DeepSeekConfigurationError("DEEPSEEK_ENABLED 只能为 true、false、1 或 0。")
    return cleaned in {"true", "1"}


def _read_local_config() -> dict:
    """Read only the project-local file, not the caller's working directory.

    TOML errors may contain secret text, so never expose their original messages.
    A missing file keeps the existing environment-only setup valid.
    """
    try:
        with LOCAL_CONFIG_PATH.open("rb") as stream:
            content = stream.read(65537)
    except FileNotFoundError:
        return {}
    except OSError:
        raise DeepSeekConfigurationError("无法读取 deepseek.local.toml，请检查文件权限。") from None
    try:
        if len(content) > 65536:
            raise ValueError("file too large")
        data = tomllib.loads(content.decode("utf-8-sig"))
        if set(data) != {"deepseek"} or not isinstance(data["deepseek"], dict):
            raise ValueError("expected one deepseek table")
        values = data["deepseek"]
        if not set(values) <= set(_CONFIG_FIELDS):
            raise ValueError("unknown settings")
        return values
    except (ValueError, UnicodeError):
        raise DeepSeekConfigurationError("deepseek.local.toml 格式不合法，请参照 deepseek.example.toml；仅支持 [deepseek] 下的配置字段。") from None


def _setting(local: dict, name: str):
    """Environment presence wins even for empty strings; do not reuse a stale key."""
    env_name, default, kind = _CONFIG_FIELDS[name]
    if env_name in os.environ:
        raw = os.environ[env_name]
        try:
            if kind is bool:
                return _enabled_env(raw)
            return kind(raw.strip())
        except (ValueError, TypeError):
            raise DeepSeekConfigurationError(f"{env_name} 配置类型不合法。") from None
    value = local.get(name, default)
    valid = type(value) is kind or (kind is float and type(value) in (int, float))
    if not valid:
        raise DeepSeekConfigurationError(f"deepseek.local.toml 中 {name} 的类型不合法。")
    return value.strip() if kind is str else value


@dataclass(frozen=True, slots=True)
class DeepSeekSettings:
    """All agents share these settings; changing them requires a service restart."""

    api_key: str = field(repr=False)
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-flash"
    timeout_seconds: float = 90.0
    # Kept only to accept older local TOML files; DeepSeekClient does not send it.
    max_tokens: int = 8192
    max_input_chars: int = 120000

    def __post_init__(self) -> None:
        if not isinstance(self.api_key, str) or not self.api_key.strip() or any(c.isspace() for c in self.api_key):
            raise DeepSeekConfigurationError("请在 deepseek.local.toml 的 api_key 或 DEEPSEEK_API_KEY 环境变量中设置有效密钥。")
        parsed = urlsplit(self.base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in ("", "/v1")):
            raise DeepSeekConfigurationError("DEEPSEEK_BASE_URL / base_url 必须是 HTTPS 基础地址，可带 /v1，不能含凭据或查询参数。")
        if not self.model.strip() or len(self.model) > 100 or any(c.isspace() for c in self.model):
            raise DeepSeekConfigurationError("DEEPSEEK_MODEL / model 必须是有效的模型标识。")
        if (not math.isfinite(self.timeout_seconds) or not 1 <= self.timeout_seconds <= 600
                or not 1000 <= self.max_input_chars <= 500000):
            raise DeepSeekConfigurationError("DeepSeek 超时或输入长度配置超出允许范围。")

    @classmethod
    def from_environment(cls) -> DeepSeekSettings | None:
        """Load defaults < local TOML < environment, retaining the existing entry point."""
        # An explicit environment opt-out works even when the local file is broken.
        if "DEEPSEEK_ENABLED" in os.environ and not _enabled_env(os.environ["DEEPSEEK_ENABLED"]):
            return None
        local = _read_local_config()
        if not _setting(local, "enabled"):
            return None
        try:
            values = {name: _setting(local, name) for name in _CONFIG_FIELDS if name != "enabled"}
            values["base_url"] = values["base_url"].rstrip("/")
            return cls(**values)
        except (ValueError, TypeError):
            raise DeepSeekConfigurationError("DeepSeek 配置不合法，请检查本地文件和环境变量。") from None
