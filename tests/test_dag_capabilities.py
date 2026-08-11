"""
AgentBus Phase 3 Tests: DAG Task Orchestration + Capability Discovery
Run: pytest tests/test_dag_capabilities.py -v

Requires a running AgentBus server (default http://127.0.0.1:7700).
"""
import os
import time
import requests
import pytest

BASE = os.getenv("AGENT_BUS_URL", "http://127.0.0.1:7700")
MASTER_KEY = os.getenv("AGENT_BUS_MASTER_KEY", "changeme-on-deploy")
TEST_PREFIX = "test_dag"

def auth_header(key):
    return {"X-API-Key": key}


@pytest.fixture(scope="module")
def dag_agents():
    """Register agents for DAG tests with capabilities."""
    s = requests.Session()
    agents = {}

    for name, caps in [
        ("alice", ["code_review", "deploy", "patrol"]),
        ("bob", ["deploy", "test"]),
        ("carol", ["code_review", "test", "patrol"]),
    ]:
        full = f"{TEST_PREFIX}_{name}"
        resp = s.post(f"{BASE}/agents/register",
            headers={"X-API-Key": MASTER_KEY},
            json={"name": full, "description": f"DAG test {name}", "capabilities": caps},
            timeout=10)
        if resp.status_code == 409:
            # Already exists from previous run — get fresh key
            # Force re-register not available, so we'll use a new unique name
            full = f"{TEST_PREFIX}_{name}_{int(time.time()) % 100000}"
            resp = s.post(f"{BASE}/agents/register",
                headers={"X-API-Key": MASTER_KEY},
                json={"name": full, "description": f"DAG test {name}", "capabilities": caps},
                timeout=10)
        assert resp.status_code == 200, f"Failed to register {name}: {resp.text}"
        agents[name] = {"name": full, "key": resp.json()["api_key"], "id": resp.json()["agent_id"]}

    yield agents

    # Cleanup
    for a in agents.values():
        try:
            s.delete(f"{BASE}/agents/{a['name']}",
                headers={"X-API-Key": MASTER_KEY}, timeout=5)
        except Exception:
            pass


# ── DAG Chain Tests ──────────────────────────────────────

class TestDAGChain:
    """Test that tasks with dependencies auto-advance when upstream completes."""

    def test_linear_chain_A_B_C(self, dag_agents):
        """Create A→B→C chain. When A completes, B auto-advances. When B completes, C auto-advances."""
        echo_key = dag_agents["alice"]["key"]
        assignee = dag_agents["bob"]["name"]

        # Step 1: Create task A (no deps, assigned to bob)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "DAG-A: Setup",
                "assignee": assignee,
                "tags": ["dag_test"],
            }, timeout=5)
        assert resp.status_code == 200, f"Create A failed: {resp.text}"
        task_a = resp.json()["id"]
        assert resp.json()["status"] == "assigned"

        # Step 2: Create task B (depends on A, auto_advance=True)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "DAG-B: Build",
                "assignee": assignee,
                "depends_on": [task_a],
                "auto_advance": True,
                "tags": ["dag_test"],
            }, timeout=5)
        assert resp.status_code == 200, f"Create B failed: {resp.text}"
        task_b = resp.json()["id"]
        # B should be pending because A is not done yet
        assert resp.json()["status"] == "pending", f"B should be pending, got {resp.json()['status']}"

        # Step 3: Create task C (depends on B, auto_advance=True)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "DAG-C: Deploy",
                "assignee": assignee,
                "depends_on": [task_b],
                "auto_advance": True,
                "tags": ["dag_test"],
            }, timeout=5)
        assert resp.status_code == 200, f"Create C failed: {resp.text}"
        task_c = resp.json()["id"]
        assert resp.json()["status"] == "pending"

        # Step 4: Complete A → should auto-advance B
        resp = requests.patch(f"{BASE}/tasks/{task_a}",
            headers=auth_header(echo_key),
            json={"status": "done", "result": "A done"}, timeout=5)
        assert resp.status_code == 200
        dag_adv = resp.json().get("dag_advanced", [])
        assert task_b in dag_adv, f"B should be in dag_advanced, got {dag_adv}"

        # Verify B is now assigned
        resp = requests.get(f"{BASE}/tasks/{task_b}",
            headers=auth_header(echo_key), timeout=5)
        assert resp.json()["status"] == "assigned", f"B should be assigned after A done"

        # C should still be pending (B not done yet)
        resp = requests.get(f"{BASE}/tasks/{task_c}",
            headers=auth_header(echo_key), timeout=5)
        assert resp.json()["status"] == "pending", "C should still be pending"

        # Step 5: Complete B → should auto-advance C
        resp = requests.patch(f"{BASE}/tasks/{task_b}",
            headers=auth_header(echo_key),
            json={"status": "done", "result": "B done"}, timeout=5)
        assert resp.status_code == 200
        dag_adv = resp.json().get("dag_advanced", [])
        assert task_c in dag_adv, f"C should be in dag_advanced, got {dag_adv}"

        # Verify C is now assigned
        resp = requests.get(f"{BASE}/tasks/{task_c}",
            headers=auth_header(echo_key), timeout=5)
        assert resp.json()["status"] == "assigned", "C should be assigned after B done"

    def test_parallel_merge(self, dag_agents):
        """Two independent tasks A1, A2 → merge task M depends on both.
        M should NOT advance until both A1 and A2 are done."""
        echo_key = dag_agents["alice"]["key"]
        assignee = dag_agents["bob"]["name"]

        # Create A1 and A2
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "P-A1", "assignee": assignee, "tags": ["dag_test"]}, timeout=5)
        a1 = resp.json()["id"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "P-A2", "assignee": assignee, "tags": ["dag_test"]}, timeout=5)
        a2 = resp.json()["id"]

        # Create M depending on both
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "P-Merge",
                "assignee": assignee,
                "depends_on": [a1, a2],
                "auto_advance": True,
                "tags": ["dag_test"],
            }, timeout=5)
        m = resp.json()["id"]
        assert resp.json()["status"] == "pending"

        # Complete A1 → M should NOT advance (A2 still pending)
        requests.patch(f"{BASE}/tasks/{a1}",
            headers=auth_header(echo_key),
            json={"status": "done"}, timeout=5)

        resp = requests.get(f"{BASE}/tasks/{m}",
            headers=auth_header(echo_key), timeout=5)
        assert resp.json()["status"] == "pending", "M should still be pending after only A1 done"

        # Complete A2 → M should now advance
        resp = requests.patch(f"{BASE}/tasks/{a2}",
            headers=auth_header(echo_key),
            json={"status": "done"}, timeout=5)
        dag_adv = resp.json().get("dag_advanced", [])
        assert m in dag_adv, f"M should be advanced after both deps done, got {dag_adv}"

    def test_no_auto_advance_flag(self, dag_agents):
        """auto_advance=False → downstream stays pending even when deps are done."""
        echo_key = dag_agents["alice"]["key"]
        assignee = dag_agents["bob"]["name"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "NA-Upstream", "assignee": assignee, "tags": ["dag_test"]}, timeout=5)
        upstream = resp.json()["id"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "NA-Downstream",
                "assignee": assignee,
                "depends_on": [upstream],
                "auto_advance": False,  # Explicitly disabled
                "tags": ["dag_test"],
            }, timeout=5)
        downstream = resp.json()["id"]
        assert resp.json()["status"] == "pending"

        # Complete upstream
        resp = requests.patch(f"{BASE}/tasks/{upstream}",
            headers=auth_header(echo_key),
            json={"status": "done"}, timeout=5)
        dag_adv = resp.json().get("dag_advanced", [])
        assert downstream not in dag_adv, "Downstream should NOT advance without auto_advance"

        resp = requests.get(f"{BASE}/tasks/{downstream}",
            headers=auth_header(echo_key), timeout=5)
        assert resp.json()["status"] == "pending"


# ── Capability Discovery Tests ───────────────────────────

class TestCapabilities:

    def test_register_with_capabilities(self, dag_agents):
        """Agent registration stores capabilities."""
        # Already registered in fixture — verify via /agents/me
        alice_key = dag_agents["alice"]["key"]
        resp = requests.get(f"{BASE}/agents/me",
            headers=auth_header(alice_key), timeout=5)
        assert resp.status_code == 200
        caps = resp.json().get("capabilities", [])
        assert "code_review" in caps
        assert "deploy" in caps

    def test_find_by_skill(self, dag_agents):
        """GET /agents/capabilities?skill=code_review returns matching agents."""
        echo_key = dag_agents["alice"]["key"]

        resp = requests.get(f"{BASE}/agents/capabilities?skill=code_review",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_filter"] == "code_review"
        names = [a["name"] for a in data["agents"]]
        # Both alice and carol have code_review
        assert dag_agents["alice"]["name"] in names
        assert dag_agents["carol"]["name"] in names

    def test_find_by_nonexistent_skill(self, dag_agents):
        """GET /agents/capabilities?skill=nonexistent returns empty."""
        echo_key = dag_agents["alice"]["key"]

        resp = requests.get(f"{BASE}/agents/capabilities?skill=flying",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        assert resp.json()["count"] == 0

    def test_update_capabilities(self, dag_agents):
        """PATCH /agents/me/capabilities updates skill list."""
        bob_key = dag_agents["bob"]["key"]

        resp = requests.patch(f"{BASE}/agents/me/capabilities",
            headers=auth_header(bob_key),
            json={"skills": ["deploy", "test", "code_review", "monitoring"]}, timeout=5)
        assert resp.status_code == 200
        assert "monitoring" in resp.json()["capabilities"]

        # Verify persisted
        resp = requests.get(f"{BASE}/agents/me",
            headers=auth_header(bob_key), timeout=5)
        assert "monitoring" in resp.json().get("capabilities", [])

    def test_list_all_capabilities(self, dag_agents):
        """GET /agents/capabilities (no filter) returns all agents with caps."""
        echo_key = dag_agents["alice"]["key"]

        resp = requests.get(f"{BASE}/agents/capabilities",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["skill_filter"] is None
        assert data["count"] >= 3  # at least our 3 test agents
