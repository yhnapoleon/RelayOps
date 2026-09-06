"""Lazy OpenAI-compatible model clients. All credentials and model names are explicitly configured."""

from __future__ import annotations

from typing import Any

from core.config import get_config

try:
    from langchain_openai import ChatOpenAI  # type: ignore

    _LANGCHAIN_AVAILABLE = True
except ImportError:
    ChatOpenAI = None  # type: ignore[assignment]
    _LANGCHAIN_AVAILABLE = False


class AgentDependencyError(Exception):
    """The agent extra isn't installed (``pip install .[agent]``)."""


class LlmNotConfiguredError(Exception):
    """The ``llm:`` config block is missing endpoint and/or bearer token."""


def get_chat_model(*, tier: str = "default", **overrides: Any):
    """RelayOps get chat model."""
    config = get_config()
    if tier == "light":
        model = config.llm_light_model or config.llm_model
    else:
        model = config.llm_model
    # Explicit override beats the tier (the old code had this backwards,
    # which made per-call model selection impossible).
    model = overrides.pop("model", None) or model
    return _build_client(config, model=model, **overrides)


def get_vision_model(**overrides: Any):
    """Chat client bound to the gateway's vision model (``llm.vision_model``,
    explicitly configured). Same OpenAI wire format — multimodal
    messages carry ``image_url`` content parts."""
    config = get_config()
    model = overrides.pop("model", None) or config.llm_vision_model
    return _build_client(config, model=model, **overrides)


def _build_client(config, *, model: str, **overrides: Any):
    if not config.llm_configured:
        raise LlmNotConfiguredError(
            "Set llm.endpoint and a bearer token (env RELAYOPS_LLM_API_KEY "
            "and RELAYOPS_LLM_MODEL) before using agent features."
        )

    if not _LANGCHAIN_AVAILABLE:
        raise AgentDependencyError(
            "langchain-openai is not installed; install the agent extra: pip install .[agent]"
        )

    import httpx

    # The coordinator is OpenAI-compatible with chat at /v1/chat/completions
    # base_url, so base_url must end with /v1 — normalize so the config can
    # hold the bare host either way.
    endpoint = config.llm_endpoint
    if not endpoint.rstrip("/").endswith("/v1"):
        endpoint = endpoint.rstrip("/") + "/v1"

    # default trust store doesn't know — same situation as MmpInterface.
    # ca_bundle_path (when set) wins over the verify_ssl boolean.
    verify: Any = config.llm_ca_bundle_path or config.llm_verify_ssl
    kwargs: dict[str, Any] = {
        "base_url": endpoint,
        "api_key": config.llm_bearer_token,
        "model": model,
        "timeout": config.llm_timeout_seconds,
        "http_client": httpx.Client(verify=verify, timeout=config.llm_timeout_seconds),
        "http_async_client": httpx.AsyncClient(verify=verify, timeout=config.llm_timeout_seconds),
    }
    kwargs.update(overrides)
    return ChatOpenAI(**kwargs)
