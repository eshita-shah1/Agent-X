"""LLM providers behind one tiny interface: `complete(system, user) -> text`.

A provider only turns a prompt into text. It has no tools, no database session and no access to any trading service, so
whatever a model answers can never place, change or cancel anything. Credentials are SecretStr values that are never
logged or returned.

  * OpenAICompatibleProvider: POST {LLM_BASE_URL}/chat/completions with the standard library (no new dependency). Works
    with local servers such as Ollama or LM Studio (no key, no paid account) and with hosted OpenAI-compatible APIs.
  * AnthropicProvider: the Claude API through the official `anthropic` SDK, imported only when this provider is
    selected. The SDK is an optional install (`pip install anthropic`); without it the provider reports
    SDK_NOT_INSTALLED and explanations stay rule-based.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from typing import Protocol

from app.core.config import Settings

DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
# Models that accept `output_config.effort` and the server-side refusal fallback ("default" mode).
_ANTHROPIC_CURRENT = {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}


class ProviderError(Exception):
    """`code` is a short, stable label (stored with the explanation); `retryable` covers timeouts, 429 and 5xx."""

    def __init__(self, code: str, retryable: bool = False):
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class Provider(Protocol):
    name: str
    model: str
    handles_retries: bool

    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleProvider:
    name = "openai_compatible"
    handles_retries = False

    def __init__(self, settings: Settings):
        self.model = settings.llm_model or ""
        self.url = settings.llm_base_url.rstrip("/") + "/chat/completions"
        self.timeout = settings.llm_timeout_seconds
        self.max_tokens = settings.llm_max_output_tokens
        self._key = settings.llm_api_key

    def complete(self, system: str, user: str) -> str:
        body = json.dumps({
            "model": self.model, "max_tokens": self.max_tokens, "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self._key is not None and self._key.get_secret_value():
            headers["Authorization"] = f"Bearer {self._key.get_secret_value()}"
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310 (http(s) only, validated)
                payload = json.loads(response.read(1_000_000).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            exc.close()  # release the error response's connection
            raise ProviderError(f"HTTP_{exc.code}", retryable=exc.code == 429 or exc.code >= 500) from None
        except (TimeoutError, socket.timeout):
            raise ProviderError("TIMEOUT", retryable=True) from None
        except urllib.error.URLError as exc:
            timed_out = isinstance(exc.reason, (TimeoutError, socket.timeout))
            raise ProviderError("TIMEOUT" if timed_out else "NETWORK_ERROR", retryable=True) from None
        except (ValueError, UnicodeDecodeError):
            raise ProviderError("MALFORMED_RESPONSE") from None
        try:
            return payload["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            raise ProviderError("MALFORMED_RESPONSE") from None


class AnthropicProvider:
    name = "anthropic"
    handles_retries = True  # the SDK retries 408/409/429/5xx and connection errors itself (max_retries)

    def __init__(self, settings: Settings):
        self.model = settings.llm_model or DEFAULT_ANTHROPIC_MODEL
        self.settings = settings

    def complete(self, system: str, user: str) -> str:
        try:
            import anthropic
        except ImportError:
            raise ProviderError("SDK_NOT_INSTALLED") from None
        s = self.settings
        client = anthropic.Anthropic(
            api_key=s.llm_api_key.get_secret_value() if s.llm_api_key else None,
            base_url=s.llm_base_url or None, timeout=s.llm_timeout_seconds, max_retries=s.llm_max_retries,
        )
        extra_body: dict = {}
        extra_headers: dict = {}
        if self.model in _ANTHROPIC_CURRENT:
            # Short, factual rewording: low effort. On a policy decline the API re-runs the request on a fallback model.
            extra_body = {"output_config": {"effort": "low"}, "fallbacks": "default"}
            extra_headers = {"anthropic-beta": "server-side-fallback-2026-07-01"}
        try:
            message = client.messages.create(
                model=self.model, max_tokens=s.llm_max_output_tokens, system=system,
                messages=[{"role": "user", "content": user}], extra_body=extra_body or None,
                extra_headers=extra_headers or None,
            )
        except anthropic.APITimeoutError:
            raise ProviderError("TIMEOUT") from None
        except anthropic.APIConnectionError:
            raise ProviderError("NETWORK_ERROR") from None
        except anthropic.RateLimitError:
            raise ProviderError("HTTP_429") from None
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"HTTP_{exc.status_code}") from None
        if message.stop_reason == "refusal":
            raise ProviderError("REFUSED")
        return "".join(block.text for block in message.content if block.type == "text")


def build_provider(settings: Settings) -> Provider | None:
    """The configured provider, or None when disabled or misconfigured (see Settings.llm_config_problems)."""
    if settings.llm_provider == "disabled" or settings.llm_config_problems():
        return None
    if settings.llm_provider == "openai_compatible":
        return OpenAICompatibleProvider(settings)
    return AnthropicProvider(settings)
