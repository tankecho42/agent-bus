"""
AgentBus E2E Integration Test: Full team collaboration pipeline.

Simulates the complete chain:
  1. Echo creates a multi-stage pipeline (3 tasks with DAG deps)
  2. Capability-based assignment (find the right agent for each task)
  3. Agent picks up and starts task
  4. Agent completes task → downstream auto-advances
  5. Inter-agent messaging between pipeline stages
  6. Full lifecycle verification

Run: pytest tests/test_e2e.py -v

Requires a running AgentBus server (default http://127.0.0.1:7700).
"""
import os
import time
import json
import requests
import pytest

BASE = os.getenv("AGENT_BUS_URL", "http://127.0.0.1:7700")
MASTER_KEY = os.getenv("AGENT_BUS_MASTER_KEY", "changeme-on-deploy")
TEST_PREFIX = f"e2e_{int(time.time()) % 100000}"


def auth(key):
    return {"X-API-Key": key}


@pytest.fixture(scope="module")
def team():
    """Provision a 3-agent team: Echo (coordinator), Alice (coder), Bob (tester)."""
    s = requests.Session()
    agents = {}

    specs = [
        ("echo",  ["coordinator", "task_management"], "Team coordinator"),
        ("alice", ["coding", "code_review", "deploy"], "Senior coder"),
        ("bob",   ["testing", "deploy", "code_review"], "QA engineer"),
    ]

    for name, caps, desc in specs:
        full = f"{TEST_PREFIX}_{name}"
        resp = s.post(f"{BASE}/agents/register",
            headers={"X-API-Key": MASTER_KEY},
            json={"name": full, "description": desc, "capabilities": caps},
            timeout=10)
        assert resp.status_code == 200, f"Register {name} failed: {resp.text}"
        agents[name] = {
            "name": full,
            "key": resp.json()["api_key"],
            "id": resp.json()["agent_id"],
            "caps": caps,
        }

    yield agents

    # Cleanup
    for a in agents.values():
        try:
            s.delete(f"{BASE}/agents/{a['name']}",
                headers={"X-API-Key": MASTER_KEY}, timeout=5)
        except Exception:
            pass


class TestE2EFullPipeline:
    """End-to-end: Echo → Alice → Bob DAG pipeline with messaging."""

    def test_01_echo_creates_pipeline(self, team):
        """Echo creates a 3-stage pipeline: Code → Review → Test.

        Stage 1 (Code): no deps, assigned to Alice
        Stage 2 (Review): depends on Stage 1, auto_advance, assigned to Bob
        Stage 3 (Test): depends on Stage 2, auto_advance, assigned to Bob
        """
        echo_key = team["echo"]["key"]
        alice_name = team["alice"]["name"]
        bob_name = team["bob"]["name"]

        # Stage 1: Write code
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={
                "title": "E2E: Implement feature X",
                "description": "Write the core module",
                "assignee": alice_name,
                "priority": 5,
                "tags": ["e2e", "feature-x"],
            }, timeout=5)
        assert resp.status_code == 200, f"Create stage 1: {resp.text}"
        task_code = resp.json()["id"]
        assert resp.json()["status"] == "assigned"
        self.task_code = task_code

        # Stage 2: Review (depends on code, auto-advance)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={
                "title": "E2E: Review feature X",
                "description": "Code review after implementation",
                "assignee": bob_name,
                "depends_on": [task_code],
                "auto_advance": True,
                "priority": 4,
                "tags": ["e2e", "feature-x"],
            }, timeout=5)
        assert resp.status_code == 200, f"Create stage 2: {resp.text}"
        task_review = resp.json()["id"]
        assert resp.json()["status"] == "pending"  # blocked by code task
        self.task_review = task_review

        # Stage 3: Test (depends on review, auto-advance)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={
                "title": "E2E: Test feature X",
                "description": "Run test suite",
                "assignee": bob_name,
                "depends_on": [task_review],
                "auto_advance": True,
                "priority": 3,
                "tags": ["e2e", "feature-x"],
            }, timeout=5)
        assert resp.status_code == 200
        task_test = resp.json()["id"]
        assert resp.json()["status"] == "pending"
        self.task_test = task_test

    def test_02_capability_based_assignment(self, team):
        """Verify capability discovery finds the right agents."""
        echo_key = team["echo"]["key"]

        # Find agents with coding skill
        resp = requests.get(f"{BASE}/agents/capabilities?skill=coding",
            headers=auth(echo_key), timeout=5)
        assert resp.status_code == 200
        coders = [a["name"] for a in resp.json()["agents"]]
        assert team["alice"]["name"] in coders
        assert team["bob"]["name"] not in coders

        # Find agents with testing skill
        resp = requests.get(f"{BASE}/agents/capabilities?skill=testing",
            headers=auth(echo_key), timeout=5)
        assert resp.status_code == 200
        testers = [a["name"] for a in resp.json()["agents"]]
        assert team["bob"]["name"] in testers
        assert team["alice"]["name"] not in testers

    def test_03_alice_starts_and_completes_code(self, team):
        """Alice picks up the code task and completes it.
        This should auto-advance the review task."""
        alice_key = team["alice"]["key"]
        echo_key = team["echo"]["key"]

        # First recreate tasks fresh (since test_01's instance vars don't persist)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={
                "title": "E2E-Live: Code module",
                "assignee": team["alice"]["name"],
                "priority": 5,
                "tags": ["e2e_live"],
            }, timeout=5)
        assert resp.status_code == 200
        t_code = resp.json()["id"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={
                "title": "E2E-Live: Review",
                "assignee": team["bob"]["name"],
                "depends_on": [t_code],
                "auto_advance": True,
                "tags": ["e2e_live"],
            }, timeout=5)
        t_review = resp.json()["id"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={
                "title": "E2E-Live: Test",
                "assignee": team["bob"]["name"],
                "depends_on": [t_review],
                "auto_advance": True,
                "tags": ["e2e_live"],
            }, timeout=5)
        t_test = resp.json()["id"]

        # Alice starts the task
        resp = requests.patch(f"{BASE}/tasks/{t_code}",
            headers=auth(alice_key),
            json={"status": "in_progress"}, timeout=5)
        assert resp.status_code == 200

        # Alice completes the task
        resp = requests.patch(f"{BASE}/tasks/{t_code}",
            headers=auth(alice_key),
            json={"status": "done", "result": "Module implemented with 100% coverage"}, timeout=5)
        assert resp.status_code == 200

        # Verify DAG auto-advanced the review task
        dag_adv = resp.json().get("dag_advanced", [])
        assert t_review in dag_adv, f"Review should auto-advance, got {dag_adv}"

        # Check review is now assigned
        resp = requests.get(f"{BASE}/tasks/{t_review}",
            headers=auth(echo_key), timeout=5)
        assert resp.json()["status"] == "assigned"

        # Test task should still be pending
        resp = requests.get(f"{BASE}/tasks/{t_test}",
            headers=auth(echo_key), timeout=5)
        assert resp.json()["status"] == "pending"

        # Save for next test
        self._t_code = t_code
        self._t_review = t_review
        self._t_test = t_test

    def test_04_bob_reviews_and_tests(self, team):
        """Bob completes review → test auto-advances → Bob completes test.
        Full pipeline finishes."""
        bob_key = team["bob"]["key"]
        echo_key = team["echo"]["key"]

        # Recreate pipeline (self._t_* don't persist across test calls reliably)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={"title": "E2E-04: Code", "assignee": team["alice"]["name"], "tags": ["e2e_04"]},
            timeout=5)
        t_code = resp.json()["id"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={"title": "E2E-04: Review", "assignee": team["bob"]["name"],
                  "depends_on": [t_code], "auto_advance": True, "tags": ["e2e_04"]},
            timeout=5)
        t_review = resp.json()["id"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={"title": "E2E-04: Test", "assignee": team["bob"]["name"],
                  "depends_on": [t_review], "auto_advance": True, "tags": ["e2e_04"]},
            timeout=5)
        t_test = resp.json()["id"]

        # Complete code to advance review
        resp = requests.patch(f"{BASE}/tasks/{t_code}",
            headers=auth(team["alice"]["key"]),
            json={"status": "done"}, timeout=5)

        # Bob starts review
        resp = requests.patch(f"{BASE}/tasks/{t_review}",
            headers=auth(bob_key),
            json={"status": "in_progress"}, timeout=5)
        assert resp.status_code == 200

        # Bob approves review
        resp = requests.patch(f"{BASE}/tasks/{t_review}",
            headers=auth(bob_key),
            json={"status": "done", "result": "LGTM, approved"}, timeout=5)
        assert resp.status_code == 200

        # Test should auto-advance
        dag_adv = resp.json().get("dag_advanced", [])
        assert t_test in dag_adv

        # Bob starts test
        resp = requests.patch(f"{BASE}/tasks/{t_test}",
            headers=auth(bob_key),
            json={"status": "in_progress"}, timeout=5)
        assert resp.status_code == 200

        # Bob finishes test
        resp = requests.patch(f"{BASE}/tasks/{t_test}",
            headers=auth(bob_key),
            json={"status": "done", "result": "All tests pass"}, timeout=5)
        assert resp.status_code == 200

    def test_05_inter_agent_messaging(self, team):
        """Echo broadcasts a status request; Alice responds via DM."""
        echo_key = team["echo"]["key"]
        alice_key = team["alice"]["key"]
        alice_name = team["alice"]["name"]

        # Echo broadcasts
        resp = requests.post(f"{BASE}/messages",
            headers=auth(echo_key),
            json={"subject": "Status check", "body": "Everyone report in", "priority": 2},
            timeout=5)
        assert resp.status_code == 200
        assert resp.json()["channel"] == "broadcast"
        msg_broadcast = resp.json()["id"]

        # Alice reads broadcast
        resp = requests.get(f"{BASE}/messages/inbox",
            headers=auth(alice_key), timeout=5)
        assert resp.status_code == 200
        msgs = resp.json()["messages"]
        assert any(m["id"] == msg_broadcast for m in msgs)

        # Alice DMs Echo
        resp = requests.post(f"{BASE}/messages",
            headers=auth(alice_key),
            json={"to": team["echo"]["name"], "subject": "Re: Status", "body": "All good here"},
            timeout=5)
        assert resp.status_code == 200
        assert resp.json()["channel"] == "dm"

    def test_06_heartbeat_and_health(self, team):
        """Agents send heartbeats; health endpoint reflects activity."""
        alice_key = team["alice"]["key"]

        # Alice sends heartbeat
        resp = requests.post(f"{BASE}/agents/me/heartbeat",
            headers=auth(alice_key),
            json={"hostname": "alice-macmini", "load_avg": 1.5, "current_task_id": "task_xyz"},
            timeout=5)
        assert resp.status_code == 200

        # Check health endpoint
        resp = requests.get(f"{BASE}/agents/health",
            headers=auth(alice_key), timeout=5)
        assert resp.status_code == 200
        health_data = resp.json()
        alice_health = [a for a in health_data["agents"] if a["name"] == team["alice"]["name"]]
        assert len(alice_health) == 1
        assert alice_health[0]["status"] == "active"

    def test_07_task_stats_verification(self, team):
        """Verify stats endpoint shows the pipeline activity."""
        echo_key = team["echo"]["key"]

        resp = requests.get(f"{BASE}/stats",
            headers=auth(echo_key), timeout=5)
        assert resp.status_code == 200
        stats = resp.json()
        assert stats["agents"] >= 3
        assert stats["messages_total"] > 0  # We sent messages in test_05

    def test_08_full_task_lifecycle_validation(self, team):
        """Run one more complete lifecycle to ensure consistency."""
        echo_key = team["echo"]["key"]
        alice_key = team["alice"]["key"]
        alice_name = team["alice"]["name"]

        # Create
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={"title": "E2E-Final: Quick task", "assignee": alice_name, "tags": ["e2e_final"]},
            timeout=5)
        assert resp.status_code == 200
        task_id = resp.json()["id"]
        assert resp.json()["status"] == "assigned"

        # Start
        resp = requests.patch(f"{BASE}/tasks/{task_id}",
            headers=auth(alice_key),
            json={"status": "in_progress"}, timeout=5)
        assert resp.status_code == 200

        # Verify my_active_tasks
        resp = requests.get(f"{BASE}/tasks/my/active",
            headers=auth(alice_key), timeout=5)
        assert resp.status_code == 200
        active = resp.json()["tasks"]
        assert any(t["id"] == task_id for t in active)

        # Complete
        resp = requests.patch(f"{BASE}/tasks/{task_id}",
            headers=auth(alice_key),
            json={"status": "done", "result": "Done"}, timeout=5)
        assert resp.status_code == 200

        # Verify it's gone from active
        resp = requests.get(f"{BASE}/tasks/my/active",
            headers=auth(alice_key), timeout=5)
        active = resp.json()["tasks"]
        assert not any(t["id"] == task_id for t in active)


class TestE2EErrorHandling:
    """Test error conditions in the full pipeline."""

    def test_dag_partial_failure_recovery(self, team):
        """If a task fails, downstream tasks stay blocked (not silently advancing)."""
        echo_key = team["echo"]["key"]
        alice_key = team["alice"]["key"]

        # Create upstream
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={"title": "E2E-Err: Upstream", "assignee": team["alice"]["name"], "tags": ["e2e_err"]},
            timeout=5)
        t_up = resp.json()["id"]

        # Create downstream (auto_advance)
        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={
                "title": "E2E-Err: Downstream",
                "assignee": team["bob"]["name"],
                "depends_on": [t_up],
                "auto_advance": True,
                "tags": ["e2e_err"],
            }, timeout=5)
        t_down = resp.json()["id"]

        # Mark upstream as FAILED (not done)
        resp = requests.patch(f"{BASE}/tasks/{t_up}",
            headers=auth(alice_key),
            json={"status": "failed", "result": "Build error"}, timeout=5)
        assert resp.status_code == 200

        # Downstream should NOT have advanced
        dag_adv = resp.json().get("dag_advanced", [])
        assert t_down not in dag_adv, "Failed upstream should NOT advance downstream"

        # Downstream should still be pending
        resp = requests.get(f"{BASE}/tasks/{t_down}",
            headers=auth(echo_key), timeout=5)
        assert resp.json()["status"] == "pending"

    def test_task_not_found_404(self, team):
        """Accessing non-existent task returns 404."""
        echo_key = team["echo"]["key"]
        resp = requests.get(f"{BASE}/tasks/task_nonexistent_99999",
            headers=auth(echo_key), timeout=5)
        assert resp.status_code == 404

    def test_invalid_status_rejected(self, team):
        """Setting an invalid status is rejected."""
        echo_key = team["echo"]["key"]
        alice_key = team["alice"]["key"]

        resp = requests.post(f"{BASE}/tasks",
            headers=auth(echo_key),
            json={"title": "E2E-Invalid: test", "assignee": team["alice"]["name"]},
            timeout=5)
        task_id = resp.json()["id"]

        resp = requests.patch(f"{BASE}/tasks/{task_id}",
            headers=auth(alice_key),
            json={"status": "bogus_status"}, timeout=5)
        assert resp.status_code == 400
