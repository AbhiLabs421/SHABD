"""
agent_service — the LLM agent loop, wired to Ollama.

Runs a SHABD-backed Agent: the model can call the demo spells as tools. Uses
an OpenAI-compatible backend pointed at Ollama Cloud (https://ollama.com/v1)
with a bearer API key, or a local Ollama at http://localhost:11434/v1.

Config (env)
------------
OLLAMA_API_KEY   bearer key for ollama.com (leave empty for local Ollama)
OLLAMA_BASE_URL  default https://ollama.com/v1
OLLAMA_MODEL     default gpt-oss:20b

Endpoints
---------
GET  /agent/config          -> which backend/model is configured
POST /agent/run  {prompt}   -> {answer, steps}
"""
from __future__ import annotations

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from common import bootstrap  # noqa: E402

from fastapi import Body, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402

from shabd import SHABD  # noqa: E402
from shabd_agent import Agent, OpenAICompatBackend  # noqa: E402
from shabd_orchestrator import Orchestrator  # noqa: E402
import demo_spells  # noqa: E402

OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "https://ollama.com/v1")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "gpt-oss:20b")
OLLAMA_API_KEY = os.environ.get("OLLAMA_API_KEY", "")

app_shabd = SHABD(
    "agent",
    secret=bootstrap.SECRET,
    require_auth=False,
    grimoire_log_path=bootstrap.audit_path("agent"),
)
demo_spells.register(app_shabd)


def _backend(model: str | None = None) -> OpenAICompatBackend:
    return OpenAICompatBackend(
        base_url=OLLAMA_BASE_URL,
        model=model or OLLAMA_MODEL,
        api_key=OLLAMA_API_KEY,
    )


# --- Orchestrator: classify a query to an intent, route to a sub-agent ---
def _build_orchestrator() -> Orchestrator:
    orch = Orchestrator(classifier=_backend(), audit_app=app_shabd,
                        fallback_intent="general")

    orch.register_intent(
        "math",
        builder=lambda d: Agent.from_shabd(
            app_shabd, llm=_backend(),
            system="You solve math and finance questions. Use the tools.", max_steps=5),
        keywords=["gst", "tax", "calculate", "add", "sum", "plus", "multiply",
                  "times", "total", "amount", "number"],
        description="Mathematics and finance calculations",
    )
    orch.register_intent(
        "text",
        builder=lambda d: Agent.from_shabd(
            app_shabd, llm=_backend(),
            system="You transform text. Use the tools.", max_steps=4),
        keywords=["reverse", "string", "text", "uppercase", "spell"],
        description="Text transformations",
    )
    orch.register_intent(
        "general",
        builder=lambda d: Agent.from_shabd(
            app_shabd, llm=_backend(),
            system="You are a helpful assistant.", max_steps=5),
        keywords=[],
        description="General questions and conversation",
    )
    return orch


api = FastAPI(title="SHABD agent_service")
api.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@api.post("/orchestrator/classify")
def orch_classify(payload: dict = Body(...)) -> dict:
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    orch = _build_orchestrator()
    intents = list(orch._intents.values())
    name, conf, used = orch.classifier.classify(query, intents)
    return {"ok": True, "intent": name, "confidence": round(conf, 2),
            "classifier": used,
            "intents": [{"name": i.name, "description": i.description} for i in intents]}


@api.post("/orchestrator/run")
def orch_run(payload: dict = Body(...)) -> dict:
    query = (payload.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query is required")
    orch = _build_orchestrator()
    try:
        res = orch.run(query, subject="ui")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"orchestrator error: {e}")
    return {"ok": True, "intent": res.intent, "confidence": round(res.confidence, 2),
            "classifier": res.classifier_used, "answer": res.answer,
            "elapsed_s": round(res.elapsed_s, 2)}


@api.get("/health")
def health() -> dict:
    return {"service": "agent", "ok": True, "model": OLLAMA_MODEL}


@api.get("/agent/config")
def config() -> dict:
    return {
        "base_url": OLLAMA_BASE_URL,
        "model": OLLAMA_MODEL,
        "api_key_set": bool(OLLAMA_API_KEY),
        "tools": list(app_shabd._spells.keys()),
    }


@api.post("/agent/run")
def run(payload: dict = Body(...)) -> dict:
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt is required")
    model = payload.get("model")
    system = payload.get("system", "You are a helpful assistant. Use tools when needed.")
    agent = Agent.from_shabd(app_shabd, llm=_backend(model), system=system, max_steps=6)
    token = app_shabd.issue_token("agent", scopes=["read", "write"], ttl=600)
    try:
        result = agent.run(prompt, token=token)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM backend error: {e}")
    return {
        "ok": True,
        "answer": result.answer,
        "stopped_reason": result.stopped_reason,
        "steps": [
            {"n": s.n, "assistant": s.assistant, "elapsed_ms": round(s.elapsed_ms, 1)}
            for s in result.steps
        ],
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(api, host="0.0.0.0", port=bootstrap.AGENT_PORT)
