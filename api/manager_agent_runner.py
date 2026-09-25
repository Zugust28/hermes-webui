"""Default-off local manager-agent runner endpoints.

This module is a small HTTP runner implementation for the staged manager-agent
contract. It is not imported by live chat routes unless an operator explicitly
starts it as a separate process. The WebUI process should still talk to it
through ``HttpRunnerClient``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import json
import os
from pathlib import Path
import time
from typing import Any
from urllib.parse import urlsplit, unquote


MODEL_MAP_PATH = Path(__file__).resolve().parents[1] / "docs" / "rfcs" / "fixtures" / "manager-agent-model-map.example.json"


@dataclass(frozen=True)
class RoleModel:
    provider: str
    model: str
    purpose: str = ""


@dataclass(frozen=True)
class ManagerAgentModelMap:
    manager: RoleModel
    specialists: dict[str, RoleModel] = field(default_factory=dict)


def _load_json_file(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return payload


def load_model_map(path: Path | None = None) -> ManagerAgentModelMap:
    payload = _load_json_file(Path(path) if path else MODEL_MAP_PATH)

    def _role(value: Any, *, fallback_provider: str = "", fallback_model: str = "") -> RoleModel:
        data = dict(value or {})
        provider = str(data.get("provider") or fallback_provider or "").strip()
        model = str(data.get("model") or fallback_model or "").strip()
        if not model:
            raise ValueError("manager-agent model map entries require model")
        return RoleModel(
            provider=provider,
            model=model,
            purpose=str(data.get("purpose") or ""),
        )

    specialists = {
        str(role): _role(value)
        for role, value in dict(payload.get("specialists") or {}).items()
    }
    return ManagerAgentModelMap(
        manager=_role(payload.get("manager"), fallback_provider="openai-codex"),
        specialists=specialists,
    )


def _extract_text(response: Any) -> str:
    try:
        return str(response.choices[0].message.content or "").strip()
    except Exception as exc:
        raise RuntimeError("LLM response did not contain choices[0].message.content") from exc


def _json_from_text(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def call_role_llm(
    *,
    role: str,
    role_model: RoleModel,
    system: str,
    user: str,
    max_tokens: int = 1200,
    timeout: float = 120.0,
) -> str:
    from agent.auxiliary_client import call_llm

    response = call_llm(
        task=f"manager_agent_{role}",
        provider=role_model.provider or None,
        model=role_model.model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        temperature=0,
        max_tokens=max_tokens,
        timeout=timeout,
    )
    return _extract_text(response)


class ManagerAgentRunner:
    def __init__(self, *, model_map: ManagerAgentModelMap):
        self.model_map = model_map

    def plan_manager_agents(self, request: dict[str, Any]) -> dict[str, Any]:
        available = request.get("available_specialists") or []
        context = request.get("context") or {}
        user = json.dumps(
            {
                "message": request.get("message") or "",
                "workspace": request.get("workspace"),
                "profile": request.get("profile"),
                "available_specialists": available,
                "context": context,
                "metadata": request.get("metadata") or {},
            },
            ensure_ascii=True,
            indent=2,
        )
        system = (
            "You are the manager agent. Return only JSON with keys strategy, "
            "specialists, and synthesis_instructions. Use specialist objects "
            "with role, task, files, toolsets, output_schema, and optional "
            "reasoning_effort. Choose at most coding and review_research roles."
        )
        raw = call_role_llm(
            role="manager",
            role_model=self.model_map.manager,
            system=system,
            user=user,
            max_tokens=1600,
        )
        parsed = _json_from_text(raw)
        specialists = []
        raw_specs = parsed.get("specialists")
        if not isinstance(raw_specs, list) or not raw_specs:
            raw_specs = [
                {
                    "role": "coding",
                    "task": str(request.get("message") or ""),
                    "toolsets": ["terminal"],
                    "files": list((context or {}).get("files") or []),
                },
                {
                    "role": "review_research",
                    "task": "Review the plan, relevant docs/tests, and implementation risks.",
                    "toolsets": ["read-only"],
                    "files": list((context or {}).get("files") or []),
                },
            ]
        for index, item in enumerate(raw_specs):
            spec = dict(item or {})
            role = str(spec.get("role") or "coding").strip()
            role_model = self.model_map.specialists.get(role) or self.model_map.specialists.get("coding")
            specialists.append(
                {
                    "agent_id": str(spec.get("agent_id") or f"{role}-{index + 1}"),
                    "role": role,
                    "task": str(spec.get("task") or request.get("message") or ""),
                    "model": role_model.model if role_model else spec.get("model"),
                    "reasoning_effort": spec.get("reasoning_effort"),
                    "toolsets": list(spec.get("toolsets") or []),
                    "files": list(spec.get("files") or []),
                    "output_schema": dict(spec.get("output_schema") or {}),
                    "metadata": {
                        **dict(spec.get("metadata") or {}),
                        "provider": role_model.provider if role_model else "",
                    },
                }
            )
        return {
            "plan_id": str(parsed.get("plan_id") or f"mgr-{int(time.time() * 1000)}"),
            "session_id": str(request.get("session_id") or ""),
            "strategy": str(parsed.get("strategy") or "manager-planned specialist fan-out"),
            "specialists": specialists,
            "synthesis_instructions": str(parsed.get("synthesis_instructions") or "Synthesize specialist results into a concise final response."),
            "status": "planned",
            "metadata": {"runner": "local-manager-agent"},
        }

    def dispatch_specialists(self, plan: dict[str, Any]) -> dict[str, Any]:
        results = []
        for spec in list(plan.get("specialists") or []):
            item = dict(spec or {})
            role = str(item.get("role") or "coding").strip()
            role_model = self.model_map.specialists.get(role) or self.model_map.specialists.get("coding")
            if role_model is None:
                results.append({
                    "agent_id": str(item.get("agent_id") or role),
                    "role": role,
                    "status": "unsupported",
                    "summary": f"No model mapping configured for role {role}.",
                })
                continue
            user = json.dumps(
                {
                    "plan_id": plan.get("plan_id"),
                    "strategy": plan.get("strategy"),
                    "task": item.get("task"),
                    "files": item.get("files") or [],
                    "metadata": item.get("metadata") or {},
                },
                ensure_ascii=True,
                indent=2,
            )
            system = (
                "You are a specialist agent. Complete the assigned analysis or "
                "implementation work. Return only JSON with keys status, summary, "
                "output, changed_files, and confidence."
            )
            raw = call_role_llm(
                role=role,
                role_model=role_model,
                system=system,
                user=user,
                max_tokens=1800,
            )
            parsed = _json_from_text(raw)
            results.append(
                {
                    "agent_id": str(item.get("agent_id") or role),
                    "role": role,
                    "status": str(parsed.get("status") or "completed"),
                    "output": dict(parsed.get("output") or {"raw": raw}),
                    "summary": str(parsed.get("summary") or raw),
                    "changed_files": list(parsed.get("changed_files") or []),
                    "confidence": parsed.get("confidence"),
                    "metadata": {"model": role_model.model, "provider": role_model.provider},
                }
            )
        return {"results": results}

    def synthesize_manager_response(self, request: dict[str, Any]) -> dict[str, Any]:
        user = json.dumps(request, ensure_ascii=True, indent=2)
        system = (
            "You are the manager agent. Synthesize specialist results into the "
            "final answer. Return only JSON with keys status, content, and metadata."
        )
        raw = call_role_llm(
            role="manager",
            role_model=self.model_map.manager,
            system=system,
            user=user,
            max_tokens=1600,
        )
        parsed = _json_from_text(raw)
        return {
            "session_id": str(request.get("session_id") or ""),
            "status": str(parsed.get("status") or "completed"),
            "content": str(parsed.get("content") or raw),
            "results": list(request.get("results") or []),
            "metadata": {
                "runner": "local-manager-agent",
                "model": self.model_map.manager.model,
                "provider": self.model_map.manager.provider,
                **dict(parsed.get("metadata") or {}),
            },
        }


def _read_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    raw_length = handler.headers.get("Content-Length", "0")
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        raise ValueError("Invalid Content-Length")
    if length < 0 or length > 1024 * 1024:
        raise ValueError("Invalid Content-Length")
    raw = handler.rfile.read(length).decode("utf-8", errors="replace") if length else "{}"
    payload = json.loads(raw or "{}")
    if not isinstance(payload, dict):
        raise ValueError("JSON body must be an object")
    return payload


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any], *, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def make_handler(runner: ManagerAgentRunner):
    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/health":
                _send_json(self, {"ok": True, "service": "manager-agent-runner"})
                return
            _send_json(self, {"error": "not found"}, status=404)

        def do_POST(self) -> None:
            path = urlsplit(self.path).path
            try:
                body = _read_body(self)
                if path == "/v1/manager/plans":
                    _send_json(self, runner.plan_manager_agents(body))
                    return
                if path.startswith("/v1/manager/plans/") and path.endswith("/dispatch"):
                    plan_id = unquote(path[len("/v1/manager/plans/"):-len("/dispatch")].strip("/"))
                    body.setdefault("plan_id", plan_id)
                    _send_json(self, runner.dispatch_specialists(body))
                    return
                if path == "/v1/manager/synthesize":
                    _send_json(self, runner.synthesize_manager_response(body))
                    return
                _send_json(self, {"error": "not found"}, status=404)
            except Exception as exc:
                _send_json(self, {"error": str(exc)}, status=500)

        def log_message(self, fmt: str, *args: Any) -> None:
            if os.environ.get("HERMES_MANAGER_RUNNER_DEBUG"):
                super().log_message(fmt, *args)

    return _Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Hermes manager-agent local runner")
    parser.add_argument("--host", default=os.environ.get("HERMES_MANAGER_RUNNER_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("HERMES_MANAGER_RUNNER_PORT", "8797")))
    parser.add_argument("--model-map", default=os.environ.get("HERMES_MANAGER_AGENT_MODEL_MAP", str(MODEL_MAP_PATH)))
    args = parser.parse_args(argv)

    runner = ManagerAgentRunner(model_map=load_model_map(Path(args.model_map)))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(runner))
    print(f"manager-agent runner listening on http://{args.host}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
