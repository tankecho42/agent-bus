"""
AgentBus Error Recovery Module

Standalone module for detecting and recovering from:
  1. Stale tasks (in_progress too long)
  2. Dead agents (heartbeat timeout) → task reassignment
  3. Blocked DAG nodes (deps met but not advancing)

Usage:
  python3 error_recovery.py --scan              # One-shot scan + report
  python3 error_recovery.py --recover-agent <name>  # Recover specific agent
  python3 error_recovery.py --full              # Full recovery cycle

Can also be imported as a library:
  from error_recovery import ErrorRecoveryManager
"""
import os
import sys
import json
import time
import sqlite3
import argparse
import logging
import fcntl
import uuid
from contextlib import contextmanager

DB_PATH = os.getenv("AGENT_BUS_DB", "agent_bus.db")
TASK_TIMEOUT_HOURS = float(os.getenv("TASK_TIMEOUT_HOURS", "2"))
HEARTBEAT_DEAD_THRESHOLD = int(os.getenv("HEARTBEAT_DEAD_THRESHOLD", "5400"))  # 90 min
MAX_RECOVERY_RETRIES = int(os.getenv("MAX_RECOVERY_RETRIES", "3"))  # Circuit breaker: max times an agent's tasks get recovered before alerting

# Logging — no more silent swallowing
logger = logging.getLogger("error_recovery")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)s %(message)s"))
    logger.addHandler(handler)

# File lock for preventing concurrent instances (anti-thundering-herd)
LOCK_FILE = os.getenv("RECOVERY_LOCK_FILE", "/tmp/agent_bus_recovery.lock")

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except sqlite3.Error as e:
        conn.rollback()
        logger.error(f"DB error: {e}")
        raise
    finally:
        conn.close()

@contextmanager
def recovery_lock():
    """File-based lock to prevent concurrent recovery cycles (anti-thundering-herd).
    
    Uses fcntl.flock for cross-process safety. If another instance is running,
    waits up to 30 seconds then gives up gracefully.
    """
    lock_fd = None
    try:
        lock_fd = open(LOCK_FILE, 'w')
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            # Another instance is running — wait and retry
            logger.warning("Another recovery instance is running, waiting up to 30s...")
            deadline = time.time() + 30
            while time.time() < deadline:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (IOError, OSError):
                    time.sleep(2)
            else:
                logger.error("Could not acquire lock after 30s — aborting to prevent thundering herd")
                yield False  # Signal caller to skip
                return
        yield True
    finally:
        if lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except:
                pass
            lock_fd.close()
            try:
                os.remove(LOCK_FILE)
            except:
                pass


class ErrorRecoveryManager:
    """Detects and recovers from task/agent failures."""

    def __init__(self, db_path=None):
        global DB_PATH
        if db_path:
            self.db_path = db_path
        else:
            self.db_path = DB_PATH

    def scan_stale_tasks(self, timeout_hours=None):
        """Find tasks that have been in_progress longer than the timeout."""
        if timeout_hours is None:
            timeout_hours = TASK_TIMEOUT_HOURS

        threshold = time.time() - (timeout_hours * 3600)
        results = []

        try:
            with get_db() as conn:
                rows = conn.execute(
                    """SELECT t.id, t.title, t.assignee, t.started_at,
                              a.name AS assignee_name
                       FROM tasks t
                       LEFT JOIN agents a ON t.assignee = a.id
                       WHERE t.status = 'in_progress'
                         AND t.started_at IS NOT NULL
                         AND t.started_at < ?""",
                    (threshold,)
                ).fetchall()

                for r in rows:
                    age_h = (time.time() - r["started_at"]) / 3600
                    results.append({
                        "task_id": r["id"],
                        "title": r["title"],
                        "assignee": r["assignee_name"] or "unassigned",
                        "started_at": r["started_at"],
                        "age_hours": round(age_h, 1),
                    })
        except sqlite3.Error as e:
            logger.error(f"scan_stale_tasks failed: {e}")
            return []

        return results

    def mark_stale(self, task_ids):
        """Mark tasks as stale. Returns count of updated tasks."""
        if not task_ids:
            return 0

        now = time.time()
        count = 0
        try:
            with get_db() as conn:
                for tid in task_ids:
                    conn.execute(
                        "UPDATE tasks SET status = 'stale', updated_at = ? WHERE id = ? AND status = 'in_progress'",
                        (now, tid)
                    )
                    if conn.total_changes > 0:
                        conn.execute(
                            """INSERT INTO audit_events (id, timestamp, actor_id, actor_name, action, entity_type, entity_id, changes, context)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (f"ae_{int(now)}_{tid[-8:]}", now, "system", "error_recovery",
                             "mark_stale", "task", tid,
                             json.dumps({"from": "in_progress", "to": "stale"}),
                             json.dumps({"reason": "timeout"}))
                        )
                        count += 1
        except sqlite3.Error as e:
            logger.error(f"mark_stale failed: {e}")
            return count

        return count

    def find_dead_agents(self, threshold_sec=None):
        """Find agents whose heartbeat is older than threshold."""
        if threshold_sec is None:
            threshold_sec = HEARTBEAT_DEAD_THRESHOLD

        threshold = time.time() - threshold_sec
        results = []

        try:
            with get_db() as conn:
                rows = conn.execute(
                    """SELECT id, name, last_seen
                       FROM agents
                       WHERE last_seen IS NOT NULL AND last_seen < ?
                       ORDER BY last_seen""",
                    (threshold,)
                ).fetchall()

                for r in rows:
                    elapsed = time.time() - (r["last_seen"] or 0)
                    results.append({
                        "agent_id": r["id"],
                        "name": r["name"],
                        "last_seen": r["last_seen"],
                        "dead_seconds": int(elapsed),
                    })
        except sqlite3.Error as e:
            logger.error(f"find_dead_agents failed: {e}")
            return []

        return results

    def _get_recovery_count(self, conn, agent_name):
        """Circuit breaker: count how many times this agent's tasks have been recovered."""
        try:
            row = conn.execute(
                """SELECT COUNT(*) as c FROM audit_events
                   WHERE action = 'agent_crash_recovery' AND entity_id = ?
                   AND timestamp > ?""",
                (agent_name, time.time() - 86400)  # Last 24h
            ).fetchone()
            return row["c"] if row else 0
        except:
            return 0

    def recover_dead_agent(self, agent_name, force=False):
        """Reset all tasks for a dead/stale agent to pending.

        Circuit breaker: if an agent has been recovered MAX_RECOVERY_RETRIES times
        in the last 24h, skip recovery and return an alert instead. Use force=True
        to override.

        Returns dict with recovery results or alert.
        """
        recovered = []
        now = time.time()

        try:
            with get_db() as conn:
                agent = conn.execute(
                    "SELECT id, name FROM agents WHERE name = ?", (agent_name,)
                ).fetchone()
                if not agent:
                    return {"error": f"Agent '{agent_name}' not found"}

                # Circuit breaker check
                recovery_count = 0
                if not force:
                    recovery_count = self._get_recovery_count(conn, agent["id"])
                    if recovery_count >= MAX_RECOVERY_RETRIES:
                        logger.warning(
                            f"Circuit breaker tripped for {agent_name}: "
                            f"{recovery_count} recoveries in 24h (max={MAX_RECOVERY_RETRIES}). "
                            f"Manual intervention required."
                        )
                        return {
                            "agent": agent_name,
                            "circuit_breaker": True,
                            "recovery_count_24h": recovery_count,
                            "message": f"Agent {agent_name} has been recovered {recovery_count} times in 24h. "
                                       f"Manual intervention required. Use force=True to override."
                        }

                tasks = conn.execute(
                    """SELECT id, title, status FROM tasks
                       WHERE assignee = ? AND status IN ('assigned', 'in_progress', 'stale')""",
                    (agent["id"],)
                ).fetchall()

                for t in tasks:
                    conn.execute(
                        """UPDATE tasks
                           SET status = 'pending', assignee = NULL,
                               assigned_at = NULL, updated_at = ?
                           WHERE id = ?""",
                        (now, t["id"])
                    )
                    recovered.append({
                        "task_id": t["id"],
                        "title": t["title"],
                        "previous_status": t["status"],
                    })

                if recovered:
                    conn.execute(
                        """INSERT INTO audit_events (id, timestamp, actor_id, actor_name, action, entity_type, entity_id, changes, context)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (f"ae_{int(now)}_recovery_{agent_name[:8]}_{uuid.uuid4().hex[:6]}", now, "system", "error_recovery",
                         "agent_crash_recovery", "agent", agent["id"],
                         json.dumps({"recovered_tasks": len(recovered)}),
                         json.dumps({"agent_name": agent_name, "recovery_n_24h": recovery_count + 1}))
                    )
        except sqlite3.Error as e:
            logger.error(f"recover_dead_agent failed for {agent_name}: {e}")
            return {"error": str(e)}

        return {"agent": agent_name, "recovered": recovered}

    def dag_unblock_scan(self):
        """Find pending tasks whose dependencies are all done but haven't auto-advanced."""
        candidates = []

        try:
            with get_db() as conn:
                pending = conn.execute(
                    "SELECT id, title, depends_on, auto_advance, assignee FROM tasks WHERE status = 'pending'"
                ).fetchall()

                for task in pending:
                    deps = json.loads(task["depends_on"] or "[]")
                    if not deps:
                        continue

                    all_done = True
                    for dep_id in deps:
                        dep = conn.execute(
                            "SELECT status FROM tasks WHERE id = ?", (dep_id,)
                        ).fetchone()
                        if not dep or dep["status"] != "done":
                            all_done = False
                            break

                    if all_done and task["assignee"]:
                        candidates.append({
                            "task_id": task["id"],
                            "title": task["title"],
                            "auto_advance": bool(task["auto_advance"]),
                            "reason": "deps_met_but_not_advanced",
                        })
        except sqlite3.Error as e:
            logger.error(f"dag_unblock_scan failed: {e}")
            return []

        return candidates

    def force_advance(self, task_ids):
        """Force-advance tasks that have met all deps but are still pending."""
        now = time.time()
        advanced = []

        try:
            with get_db() as conn:
                for tid in task_ids:
                    conn.execute(
                        """UPDATE tasks
                           SET status = 'assigned', assigned_at = ?, updated_at = ?
                           WHERE id = ? AND status = 'pending'""",
                        (now, now, tid)
                    )
                    advanced.append(tid)

                    conn.execute(
                        """INSERT INTO audit_events (id, timestamp, actor_id, actor_name, action, entity_type, entity_id, changes, context)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (f"ae_{int(now)}_force_{tid[-8:]}", now, "system", "error_recovery",
                         "force_advance", "task", tid,
                         json.dumps({"from": "pending", "to": "assigned"}),
                         json.dumps({"reason": "dag_unblock"}))
                    )
        except sqlite3.Error as e:
            logger.error(f"force_advance failed: {e}")

        return advanced

    def full_recovery_cycle(self):
        """Run a complete recovery cycle: stale scan → dead agent recovery → DAG unblock.

        Now with:
        - File lock (anti-thundering-herd)
        - Circuit breaker (anti-infinite-loop)
        - Exception handling (no silent failures)
        """
        report = {
            "timestamp": time.time(),
            "stale_tasks": [],
            "dead_agents": [],
            "dag_unblocked": [],
            "actions_taken": [],
            "circuit_breaker_alerts": [],
            "skipped": False,
        }

        # Acquire lock — skip if another instance is running
        with recovery_lock() as got_lock:
            if not got_lock:
                report["skipped"] = True
                report["actions_taken"].append("Skipped — another recovery instance is running")
                return report

            # 1. Scan stale tasks
            try:
                stale = self.scan_stale_tasks()
                if stale:
                    stale_ids = [s["task_id"] for s in stale]
                    marked = self.mark_stale(stale_ids)
                    report["stale_tasks"] = stale
                    report["actions_taken"].append(f"Marked {marked} stale tasks")
            except Exception as e:
                logger.error(f"Stale task recovery failed: {e}")
                report["actions_taken"].append(f"⚠️ Stale task scan error: {e}")

            # 2. Find and recover dead agents (with circuit breaker)
            try:
                dead = self.find_dead_agents()
                for d in dead:
                    result = self.recover_dead_agent(d["name"])
                    if result.get("circuit_breaker"):
                        report["circuit_breaker_alerts"].append(result)
                        report["actions_taken"].append(
                            f"🚨 Circuit breaker: {d['name']} recovered too many times — needs manual check"
                        )
                    elif result.get("recovered"):
                        report["dead_agents"].append(d)
                        report["actions_taken"].append(
                            f"Recovered {len(result['recovered'])} tasks from {d['name']}"
                        )
            except Exception as e:
                logger.error(f"Dead agent recovery failed: {e}")
                report["actions_taken"].append(f"⚠️ Dead agent recovery error: {e}")

            # 3. DAG unblock
            try:
                blocked = self.dag_unblock_scan()
                if blocked:
                    block_ids = [b["task_id"] for b in blocked]
                    advanced = self.force_advance(block_ids)
                    report["dag_unblocked"] = blocked
                    report["actions_taken"].append(f"Force-advanced {len(advanced)} blocked DAG tasks")
            except Exception as e:
                logger.error(f"DAG unblock failed: {e}")
                report["actions_taken"].append(f"⚠️ DAG unblock error: {e}")

        return report


def format_report(report):
    """Pretty-print a recovery report."""
    lines = [
        f"═══ Error Recovery Report ═══",
        f"Timestamp: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(report['timestamp']))}",
        "",
    ]

    if report.get("skipped"):
        lines.append("⏭️  Skipped — another recovery instance is running.")
        return "\n".join(lines)

    if report["stale_tasks"]:
        lines.append(f"📋 Stale Tasks ({len(report['stale_tasks'])}):")
        for s in report["stale_tasks"]:
            lines.append(f"  • {s['task_id']} | {s['title']} | {s['age_hours']}h old | {s['assignee']}")

    if report["dead_agents"]:
        lines.append(f"💀 Dead Agents ({len(report['dead_agents'])}):")
        for d in report["dead_agents"]:
            mins = d["dead_seconds"] // 60
            lines.append(f"  • {d['name']} | last seen {mins}m ago")

    if report["dag_unblocked"]:
        lines.append(f"🔓 DAG Unblocked ({len(report['dag_unblocked'])}):")
        for d in report["dag_unblocked"]:
            lines.append(f"  • {d['task_id']} | {d['title']}")

    if report.get("circuit_breaker_alerts"):
        lines.append(f"\n🚨 Circuit Breaker Alerts ({len(report['circuit_breaker_alerts'])}):")
        for a in report["circuit_breaker_alerts"]:
            lines.append(f"  • {a['agent']}: {a.get('recovery_count_24h', 0)} recoveries in 24h — MANUAL CHECK NEEDED")

    if report["actions_taken"]:
        lines.append(f"\n✅ Actions Taken:")
        for a in report["actions_taken"]:
            lines.append(f"  • {a}")
    else:
        lines.append("✅ All clear — no issues detected.")

    return "\n".join(lines)


def main():
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="AgentBus Error Recovery")
    parser.add_argument("--scan", action="store_true", help="Scan and report only (no changes)")
    parser.add_argument("--full", action="store_true", help="Full recovery cycle")
    parser.add_argument("--recover-agent", type=str, help="Recover tasks for a specific agent")
    parser.add_argument("--force", action="store_true", help="Force recovery (override circuit breaker)")
    parser.add_argument("--db", type=str, default=DB_PATH, help="Database path")
    args = parser.parse_args()

    mgr = ErrorRecoveryManager(db_path=args.db)

    if args.recover_agent:
        result = mgr.recover_dead_agent(args.recover_agent, force=args.force)
        print(json.dumps(result, indent=2))
    elif args.scan:
        report = mgr.full_recovery_cycle()
        print(format_report(report))
    elif args.full:
        report = mgr.full_recovery_cycle()
        print(format_report(report))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
