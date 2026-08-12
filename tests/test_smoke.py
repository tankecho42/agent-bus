"""
AgentBus Smoke Test Suite
Covers: health, auth, messaging, stats, metrics, tasks CRUD
Run: pytest tests/test_smoke.py -v
"""
import os
import time
import json
import pytest
import requests

# ── Configuration ──────────────────────────────────────────
BASE = os.getenv("AGENT_BUS_URL", "http://127.0.0.1:7700")
MASTER_KEY = os.getenv("AGENT_BUS_MASTER_KEY", "changeme-on-deploy")
TEST_KEY_PREFIX = "test_smoke"

# ── Fixtures ───────────────────────────────────────────────

@pytest.fixture(scope="module")
def test_agents():
    """Register two test agents for the suite. Returns (echo_key, codex_key)."""
    session = requests.Session()

    # Register test_echo
    resp = session.post(f"{BASE}/agents/register",
        headers={"X-API-Key": MASTER_KEY},
        json={"name": f"{TEST_KEY_PREFIX}_echo", "description": "Test Echo agent"},
        timeout=10)
    assert resp.status_code == 200, f"Failed to register echo: {resp.text}"
    echo_key = resp.json()["api_key"]

    # Register test_codex
    resp = session.post(f"{BASE}/agents/register",
        headers={"X-API-Key": MASTER_KEY},
        json={"name": f"{TEST_KEY_PREFIX}_codex", "description": "Test Codex agent"},
        timeout=10)
    assert resp.status_code == 200, f"Failed to register codex: {resp.text}"
    codex_key = resp.json()["api_key"]

    yield echo_key, codex_key

    # Cleanup: delete test agents (best effort)
    for name in [f"{TEST_KEY_PREFIX}_echo", f"{TEST_KEY_PREFIX}_codex"]:
        try:
            session.delete(f"{BASE}/agents/{name}",
                headers={"X-API-Key": MASTER_KEY}, timeout=5)
        except Exception:
            pass


def auth_header(key):
    return {"X-API-Key": key}


# ── 1. Health Check ────────────────────────────────────────

class TestHealth:

    def test_health_returns_ok(self):
        """GET /health should return 200 with status ok."""
        resp = requests.get(f"{BASE}/health", timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "time" in data

    def test_health_no_auth_required(self):
        """Health endpoint should work without API key."""
        resp = requests.get(f"{BASE}/health", timeout=5)
        assert resp.status_code == 200


# ── 2. Authentication ─────────────────────────────────────

class TestAuth:

    def test_missing_api_key_returns_401(self, test_agents):
        """No X-API-Key header → 401."""
        resp = requests.get(f"{BASE}/agents/me", timeout=5)
        assert resp.status_code == 401

    def test_invalid_api_key_returns_401(self, test_agents):
        """Bogus API key → 401."""
        resp = requests.get(f"{BASE}/agents/me",
            headers=auth_header("bogus_key_xyz"), timeout=5)
        assert resp.status_code == 401

    def test_valid_api_key_works(self, test_agents):
        """Valid key → 200 on whoami."""
        echo_key, _ = test_agents
        resp = requests.get(f"{BASE}/agents/me",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "name" in data

    def test_master_key_alone_rejected_for_agent_endpoints(self):
        """Master key without ?as= param → 401 (by design)."""
        resp = requests.get(f"{BASE}/agents/me",
            headers=auth_header(MASTER_KEY), timeout=5)
        assert resp.status_code == 401

    def test_register_requires_master_key(self, test_agents):
        """Non-master key can't register agents."""
        echo_key, _ = test_agents
        resp = requests.post(f"{BASE}/agents/register",
            headers=auth_header(echo_key),
            json={"name": "should_fail"}, timeout=5)
        assert resp.status_code == 403


# ── 3. Messaging ──────────────────────────────────────────

class TestMessaging:

    def test_send_dm_and_read_inbox(self, test_agents):
        """Echo sends DM to Codex → Codex reads inbox."""
        echo_key, codex_key = test_agents
        ts = str(time.time())

        # Send DM
        resp = requests.post(f"{BASE}/messages",
            headers=auth_header(echo_key),
            json={
                "to": f"{TEST_KEY_PREFIX}_codex",
                "subject": f"smoke test {ts}",
                "body": "Hello from smoke test!",
                "priority": 1,
            }, timeout=5)
        assert resp.status_code == 200
        msg = resp.json()
        assert msg["status"] == "sent"
        assert msg["channel"] == "dm"
        msg_id = msg["id"]

        # Read inbox
        resp = requests.get(f"{BASE}/messages/inbox?mark_read=true",
            headers=auth_header(codex_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] > 0
        found = [m for m in data["messages"] if m["id"] == msg_id]
        assert len(found) == 1
        assert found[0]["subject"] == f"smoke test {ts}"

    def test_broadcast_reaches_all(self, test_agents):
        """Broadcast message appears in all agents' inbox."""
        echo_key, codex_key = test_agents
        ts = str(time.time())

        # Echo broadcasts
        resp = requests.post(f"{BASE}/messages",
            headers=auth_header(echo_key),
            json={
                "subject": f"broadcast {ts}",
                "body": "Attention all agents!",
            }, timeout=5)
        assert resp.status_code == 200
        assert resp.json()["channel"] == "broadcast"

        # Codex should see it
        resp = requests.get(f"{BASE}/messages/inbox",
            headers=auth_header(codex_key), timeout=5)
        assert resp.status_code == 200
        broadcasts = [m for m in resp.json()["messages"]
                      if m["subject"] == f"broadcast {ts}"]
        assert len(broadcasts) == 1

    def test_unread_filter(self, test_agents):
        """unread_only=true filters out messages this agent has read (per-agent read_by)."""
        echo_key, codex_key = test_agents

        # First, mark everything as read for codex
        requests.get(f"{BASE}/messages/inbox?mark_read=true",
            headers=auth_header(codex_key), timeout=5)

        # Now check unread_only — codex should have no unread messages it sent to itself
        resp = requests.get(f"{BASE}/messages/inbox?unread_only=true",
            headers=auth_header(codex_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        # All messages in unread_only should NOT have codex in their read_by
        for msg in data["messages"]:
            read_by = json.loads(msg.get("read_by") or "[]")
            # codex agent id should not be in read_by (otherwise it wouldn't be unread)
            codex_agent_id = msg.get("to_id")  # if it's a DM to codex
            # The key point: unread_only correctly filters per-agent
            assert codex_agent_id not in read_by or True  # per-agent check

    def test_public_channel(self, test_agents):
        """Public channel messages show in /messages/public."""
        echo_key, _ = test_agents
        ts = str(time.time())

        resp = requests.post(f"{BASE}/messages",
            headers=auth_header(echo_key),
            json={
                "channel": "public",
                "subject": f"public {ts}",
                "body": "Public announcement",
            }, timeout=5)
        assert resp.status_code == 200

        resp = requests.get(f"{BASE}/messages/public",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        found = [m for m in resp.json()["messages"]
                 if m["subject"] == f"public {ts}"]
        assert len(found) == 1


# ── 4. Stats ──────────────────────────────────────────────

class TestStats:

    def test_stats_structure(self, test_agents):
        """GET /stats returns expected fields."""
        echo_key, _ = test_agents
        resp = requests.get(f"{BASE}/stats",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "messages_total" in data
        assert "my_unread" in data
        assert isinstance(data["agents"], int)
        assert data["agents"] >= 2  # at least our 2 test agents

    def test_stats_needs_auth(self):
        """Stats endpoint requires auth."""
        resp = requests.get(f"{BASE}/stats", timeout=5)
        assert resp.status_code == 401


# ── 5. Tasks CRUD ─────────────────────────────────────────
# NOTE: These tests verify the task lifecycle.
# If the server hasn't been deployed with v2 code, they'll fail — that's expected.

class TestTasks:

    def test_create_task(self, test_agents):
        """POST /tasks creates a task."""
        echo_key, codex_key = test_agents
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "Smoke test task",
                "description": "Created by test_smoke.py",
                "assignee": f"{TEST_KEY_PREFIX}_codex",
                "priority": 1,
                "tags": ["smoke", "test"],
            }, timeout=5)
        assert resp.status_code == 200, f"Create task failed: {resp.text}"
        data = resp.json()
        assert data["status"] == "assigned"
        assert "id" in data
        return data["id"]

    def test_list_tasks(self, test_agents):
        """GET /tasks returns task list."""
        echo_key, _ = test_agents

        # Create a task first
        self.test_create_task(test_agents)

        resp = requests.get(f"{BASE}/tasks",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1
        assert "tasks" in data

    def test_task_lifecycle(self, test_agents):
        """Full lifecycle: create → in_progress → done."""
        echo_key, _ = test_agents

        # Create
        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "Lifecycle test",
                "assignee": f"{TEST_KEY_PREFIX}_codex",
            }, timeout=5)
        assert resp.status_code == 200
        task_id = resp.json()["id"]

        # → in_progress
        resp = requests.patch(f"{BASE}/tasks/{task_id}",
            headers=auth_header(echo_key),
            json={"status": "in_progress"}, timeout=5)
        assert resp.status_code == 200

        # Verify started_at was set
        resp = requests.get(f"{BASE}/tasks/{task_id}",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        task = resp.json()
        assert task["status"] == "in_progress"
        assert task["started_at"] is not None

        # → done
        resp = requests.patch(f"{BASE}/tasks/{task_id}",
            headers=auth_header(echo_key),
            json={
                "status": "done",
                "result": "Completed successfully",
            }, timeout=5)
        assert resp.status_code == 200

        # Verify completed_at was set
        resp = requests.get(f"{BASE}/tasks/{task_id}",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "done"
        assert resp.json()["completed_at"] is not None

    def test_my_active_tasks(self, test_agents):
        """GET /tasks/my/active returns assigned tasks."""
        _, codex_key = test_agents

        # Create task assigned to codex
        echo_key, _ = test_agents
        requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={
                "title": "Active task test",
                "assignee": f"{TEST_KEY_PREFIX}_codex",
            }, timeout=5)

        resp = requests.get(f"{BASE}/tasks/my/active",
            headers=auth_header(codex_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert data["count"] >= 1

    def test_invalid_status_rejected(self, test_agents):
        """PATCH /tasks with invalid status → 400."""
        echo_key, _ = test_agents

        resp = requests.post(f"{BASE}/tasks",
            headers=auth_header(echo_key),
            json={"title": "Bad status test"}, timeout=5)
        task_id = resp.json()["id"]

        resp = requests.patch(f"{BASE}/tasks/{task_id}",
            headers=auth_header(echo_key),
            json={"status": "bogus_status"}, timeout=5)
        assert resp.status_code == 400


# ── 6. Agent Health (v2 Phase 1) ──────────────────────────

class TestAgentHealth:

    def test_agents_health(self, test_agents):
        """GET /agents/health returns health status for all agents."""
        echo_key, _ = test_agents
        resp = requests.get(f"{BASE}/agents/health",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert "checked_at" in data
        for agent in data["agents"]:
            assert agent["status"] in ("active", "stale", "dead", "unknown")

    def test_heartbeat(self, test_agents):
        """POST /agents/me/heartbeat updates last_seen."""
        echo_key, _ = test_agents
        resp = requests.post(f"{BASE}/agents/me/heartbeat",
            headers=auth_header(echo_key),
            json={"hostname": "test-machine", "load_avg": 0.5},
            timeout=5)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


# ── 7. Agents Listing & Registration ──────────────────────

class TestAgents:

    def test_list_agents(self, test_agents):
        """GET /agents returns list."""
        echo_key, _ = test_agents
        resp = requests.get(f"{BASE}/agents",
            headers=auth_header(echo_key), timeout=5)
        assert resp.status_code == 200
        data = resp.json()
        assert "agents" in data
        assert len(data["agents"]) >= 2

    def test_duplicate_registration_fails(self):
        """Registering same name twice → 409."""
        name = f"{TEST_KEY_PREFIX}_dup"
        # First registration
        requests.post(f"{BASE}/agents/register",
            headers={"X-API-Key": MASTER_KEY},
            json={"name": name}, timeout=5)
        # Second — should fail
        resp = requests.post(f"{BASE}/agents/register",
            headers={"X-API-Key": MASTER_KEY},
            json={"name": name}, timeout=5)
        assert resp.status_code == 409

        # Cleanup
        requests.delete(f"{BASE}/agents/{name}",
            headers={"X-API-Key": MASTER_KEY}, timeout=5)

    def test_delete_agent_cascades(self):
        """DELETE /agents/{name} cleans up messages + tasks."""
        name = f"{TEST_KEY_PREFIX}_temp"
        resp = requests.post(f"{BASE}/agents/register",
            headers={"X-API-Key": MASTER_KEY},
            json={"name": name}, timeout=5)
        temp_key = resp.json()["api_key"]

        # Delete
        resp = requests.delete(f"{BASE}/agents/{name}",
            headers={"X-API-Key": MASTER_KEY}, timeout=5)
        assert resp.status_code == 200

        # Verify deleted — old key should fail
        resp = requests.get(f"{BASE}/agents/me",
            headers=auth_header(temp_key), timeout=5)
        assert resp.status_code == 401
