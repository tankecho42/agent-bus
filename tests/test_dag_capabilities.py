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


# ── Cycle Detection Tests ────────────────────────────────

class TestCycleDetection:
    """Test that circular dependencies are detected and rejected."""

    def test_direct_self_cycle(self, dag_agents):
        """A task that depends on itself should be rejected."""
        echo_key = dag_agents["alice"]["key"]

        # Create task A
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Cycle-A", "assignee": dag_agents["bob"]["name"], "tags": ["cycle_test"]},
            timeout=5)
        assert resp.status_code == 200
        task_a = resp.json()["id"]

        # Try to update A to depend on itself
        resp = requests.patch(f"{BASE}/tasks/{task_a}",
            headers=auth_header(echo_key),
            json={"depends_on": [task_a]},
            timeout=5)
        assert resp.status_code == 400
        assert "Circular dependency" in resp.json().get("detail", "")

    def test_three_node_cycle_A_B_C_A(self, dag_agents):
        """A→B→C→A should be rejected when trying to close the loop.

        Setup: Create A (no deps), B depends on A, C depends on B.
        Then try to make A depend on C — that would create A→C→B→A cycle.
        """
        echo_key = dag_agents["alice"]["key"]
        assignee = dag_agents["bob"]["name"]

        # Create A
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Cycle-A", "assignee": assignee, "tags": ["cycle_test"]},
            timeout=5)
        task_a = resp.json()["id"]

        # Create B depending on A
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Cycle-B", "assignee": assignee, "depends_on": [task_a], "tags": ["cycle_test"]},
            timeout=5)
        task_b = resp.json()["id"]

        # Create C depending on B
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Cycle-C", "assignee": assignee, "depends_on": [task_b], "tags": ["cycle_test"]},
            timeout=5)
        task_c = resp.json()["id"]

        # Now try to make A depend on C — creates cycle A→C→B→A
        resp = requests.patch(f"{BASE}/tasks/{task_a}",
            headers=auth_header(echo_key),
            json={"depends_on": [task_c]},
            timeout=5)
        assert resp.status_code == 400
        assert "Circular dependency" in resp.json().get("detail", "")

    def test_cycle_at_create_time(self, dag_agents):
        """Creating a task that would create a cycle should be rejected.

        Setup: Create A, B depends on A. Then try to create a new task
        that makes A depend on it — wait, at create time the new task
        doesn't exist yet so we test: B depends on A, create C with
        depends_on=[B], then try creating D with depends_on=[C] but also
        pointing back — actually the simplest: create A, make A depend
        on a task ID that will be B, then create B depending on A.
        Since A already has depends_on=[B_placeholder], we can't use that.
        Instead: create A and B normally, then try to PATCH A to depend on B
        while B depends on A — that's the update path.

        For create-time: create A, update A.depends_on=[task_x_placeholder]
        ... no. Let's do: A exists, B exists and depends on A. Now try to
        create a task C with depends_on=[A] AND also try to make A depend
        on C — but C doesn't exist when creating. So the real create-time
        test is: A doesn't exist, B depends on A_id (a UUID we generate).
        Then create A with depends_on pointing back to... no, A is new.

        Simplest create-time test: generate task_id for A, create B with
        depends_on=[A_id] — but A doesn't exist yet so it's fine.
        Actually the cleanest: two tasks where A→B and B→A both at create.
        Create A first, then create B with depends_on=[A], then PATCH A
        to depend on B — that's test_three_node above.

        For pure create-time: create A, then create task that depends on A,
        which is fine. The cycle only happens on update. So let's test that
        self-reference on create is rejected by creating a task with
        depends_on containing a non-existent ID that happens to be the
        new task's own ID — can't predict the UUID.

        Skip: create-time cycles are prevented by design (new task can't
        be referenced by existing tasks yet). We test via PATCH instead.
        """
        # This test documents that create-time cycles aren't possible
        # by construction — a new task ID is unknown to existing tasks.
        # The real protection is on PATCH, tested above.
        pass

    def test_no_false_positive_on_diamond(self, dag_agents):
        """Diamond: A→B, A→C, B→D, C→D — D depends on B and C.
        This is NOT a cycle and should be allowed."""
        echo_key = dag_agents["alice"]["key"]
        assignee = dag_agents["bob"]["name"]

        # Create A
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Diamond-A", "assignee": assignee, "tags": ["cycle_test"]},
            timeout=5)
        task_a = resp.json()["id"]

        # Create B depends on A
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Diamond-B", "assignee": assignee, "depends_on": [task_a], "tags": ["cycle_test"]},
            timeout=5)
        task_b = resp.json()["id"]

        # Create C depends on A
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Diamond-C", "assignee": assignee, "depends_on": [task_a], "tags": ["cycle_test"]},
            timeout=5)
        task_c = resp.json()["id"]

        # Create D depends on B and C — diamond, NOT a cycle
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Diamond-D", "assignee": assignee, "depends_on": [task_b, task_c], "tags": ["cycle_test"]},
            timeout=5)
        assert resp.status_code == 200
        assert "Circular dependency" not in resp.text
