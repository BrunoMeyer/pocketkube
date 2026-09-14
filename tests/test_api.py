from starlette.testclient import TestClient

from pocketkube.api import create_app


def test_discovery():
    app = create_app("docker")
    with TestClient(app) as c:
        assert c.get("/version").status_code == 200
        data = c.get("/api/v1").json()
        assert any(x["name"] == "pods" for x in data["resources"])
        data = c.get("/apis/apps/v1").json()
        assert any(x["name"] == "deployments" for x in data["resources"])


def test_exec_post_explains_websocket_requirement():
    app = create_app("docker")
    with TestClient(app) as c:
        r = c.post(
            "/api/v1/namespaces/default/pods/alpine/exec?command=true&stdout=true",
        )
        assert r.status_code == 426
        assert "WebSocket" in r.json()["message"]
