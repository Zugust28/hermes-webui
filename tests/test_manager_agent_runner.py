import json
import threading
import urllib.request


def test_manager_agent_runner_uses_model_map(monkeypatch):
    from api import manager_agent_runner as runner_mod

    model_map = runner_mod.ManagerAgentModelMap(
        manager=runner_mod.RoleModel(provider="manager-provider", model="manager-model"),
        specialists={
            "coding": runner_mod.RoleModel(provider="coding-provider", model="coding-model"),
        },
    )
    calls = []

    def fake_call_role_llm(*, role, role_model, system, user, **kwargs):
        calls.append((role, role_model.provider, role_model.model, json.loads(user)))
        if role == "manager" and "keys strategy" in system:
            return json.dumps({
                "strategy": "single specialist",
                "specialists": [{"role": "coding", "task": "Implement the fix."}],
            })
        if role == "manager":
            return json.dumps({"status": "completed", "content": "Done."})
        return json.dumps({"status": "completed", "summary": "Implemented."})

    monkeypatch.setattr(runner_mod, "call_role_llm", fake_call_role_llm)
    runner = runner_mod.ManagerAgentRunner(model_map=model_map)
    plan = runner.plan_manager_agents({"session_id": "s1", "message": "Fix it."})
    results = runner.dispatch_specialists(plan)["results"]
    response = runner.synthesize_manager_response({
        "session_id": "s1",
        "plan": plan,
        "results": results,
    })

    assert plan["specialists"][0]["model"] == "coding-model"
    assert results[0]["summary"] == "Implemented."
    assert response["content"] == "Done."
    assert [call[:3] for call in calls] == [
        ("manager", "manager-provider", "manager-model"),
        ("coding", "coding-provider", "coding-model"),
        ("manager", "manager-provider", "manager-model"),
    ]


def test_manager_agent_runner_health_endpoint():
    from api import manager_agent_runner as runner_mod

    model_map = runner_mod.ManagerAgentModelMap(
        manager=runner_mod.RoleModel(provider="test", model="test"),
    )
    server = runner_mod.ThreadingHTTPServer(
        ("127.0.0.1", 0),
        runner_mod.make_handler(runner_mod.ManagerAgentRunner(model_map=model_map)),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{server.server_port}/health", timeout=2
        ) as response:
            assert response.status == 200
            assert json.load(response) == {
                "ok": True,
                "service": "manager-agent-runner",
            }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
