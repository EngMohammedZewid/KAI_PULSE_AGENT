"""Chat model for the graph.

Default: GPT-OSS 120B on watsonx.ai, the same model the Orchestrate agent used,
so behaviour can be compared during the migration. Any OpenAI-compatible
endpoint works too. Provider packages are optional and imported lazily:

    KAI_MODEL_PROVIDER=watsonx  -> pip install langchain-ibm
                                   WATSONX_URL, WATSONX_PROJECT_ID, WATSONX_API_KEY
    KAI_MODEL_PROVIDER=openai   -> pip install langchain-openai
                                   KAI_MODEL_BASE_URL (optional), OPENAI_API_KEY
    KAI_MODEL_PROVIDER=dev      -> no LLM; echoes the user (try the app without credentials)
    KAI_MODEL_ID                -> model id (default openai/gpt-oss-120b)
"""
from __future__ import annotations

import os

from langchain_core.language_models import BaseChatModel

DEFAULT_MODEL_ID = "openai/gpt-oss-120b"


def get_chat_model() -> BaseChatModel:
    provider = os.environ.get("KAI_MODEL_PROVIDER", "watsonx").lower()
    model_id = os.environ.get("KAI_MODEL_ID", DEFAULT_MODEL_ID)

    if provider == "watsonx":
        try:
            from langchain_ibm import ChatWatsonx
        except ImportError as e:
            raise RuntimeError("KAI_MODEL_PROVIDER=watsonx needs: pip install langchain-ibm") from e
        missing = [k for k in ("WATSONX_URL", "WATSONX_PROJECT_ID", "WATSONX_API_KEY")
                   if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"Missing environment variables for watsonx.ai: {', '.join(missing)}")
        return ChatWatsonx(
            model_id=model_id,
            url=os.environ["WATSONX_URL"],
            project_id=os.environ["WATSONX_PROJECT_ID"],
            api_key=os.environ["WATSONX_API_KEY"],
            params={"temperature": 0},
        )

    if provider == "openai":
        try:
            from langchain_openai import ChatOpenAI
        except ImportError as e:
            raise RuntimeError("KAI_MODEL_PROVIDER=openai needs: pip install langchain-openai") from e
        return ChatOpenAI(model=model_id, base_url=os.environ.get("KAI_MODEL_BASE_URL") or None,
                          temperature=0)

    if provider == "dev":
        return DevEchoModel()

    raise RuntimeError(f"Unknown KAI_MODEL_PROVIDER '{provider}' (use watsonx, openai or dev).")


class DevEchoModel(BaseChatModel):
    """KAI_MODEL_PROVIDER=dev: no LLM. Echoes the user so the app and UI can be
    tried end to end without model credentials. Routing always lands on the
    general responder, and fixed nodes (uploads, "add") work as normal."""

    @property
    def _llm_type(self) -> str:
        return "dev-echo"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from langchain_core.messages import AIMessage, HumanMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        last = next((m for m in reversed(messages) if isinstance(m, HumanMessage)), None)
        text = last.content if last is not None and isinstance(last.content, str) else ""
        reply = f"(dev model, no LLM) You said: {text}" if text else "(dev model, no LLM)"
        return ChatResult(generations=[ChatGeneration(message=AIMessage(reply))])
