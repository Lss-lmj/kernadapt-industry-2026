#!/usr/bin/env python3
"""Lightweight runtime primitives for LockAgent multi-role executions.

This module intentionally stays dependency-free. It borrows the systems ideas
used by graph-based agent runtimes (state, checkpoints, handoff artifacts,
guardrails, trace spans), but keeps host-side authority explicit for Linux
resource-control experiments.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import time
import argparse
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from heapq import heappop, heappush
from pathlib import Path
from typing import Any, Callable, Iterable


SCHEMA_VERSION = "lockagent.runtime.v0.1"

ROLE_PROFILER = "ProfilerAgent"
ROLE_POLICY = "PolicyAgent"
ROLE_CRITIC = "CriticAgent"
ROLE_GUARD = "RuntimeGuard"
ROLE_EXECUTOR = "ExecutorAgent"
ROLE_CURATOR = "CuratorAgent"

CAP_READ_ARTIFACT = "read_artifact"
CAP_WRITE_ARTIFACT = "write_artifact"
CAP_READ_SYSTEM_STATE = "read_system_state"
CAP_BUILD_PROFILE = "build_profile"
CAP_CALL_LLM = "call_llm"
CAP_VALIDATE_ARTIFACT = "validate_artifact"
CAP_EVALUATE_POLICY = "evaluate_policy"
CAP_RUN_BENCHMARK = "run_benchmark"
CAP_ROOT_CONTROL = "root_control"
CAP_LOAD_SCX = "load_scx"
CAP_RESTART_SERVICE = "restart_service"
CAP_EMIT_PROVENANCE = "emit_provenance"
CAP_ATTRIBUTE_FAILURE = "attribute_failure"
CAP_CURATE_SKILL = "curate_skill"
CAP_HUMAN_APPROVAL = "human_approval"
CAP_MEMORY_READ = "memory_read"
CAP_MEMORY_WRITE = "memory_write"
CAP_SCHEDULE_AGENT = "schedule_agent"


def utc_timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _json_default(value: Any) -> str:
    if isinstance(value, Path):
        return str(value)
    return str(value)


def stable_json(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default)


def file_sha256(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_framework_adapter_bundle(checkpoint: Path) -> dict[str, Any]:
    module_name = "_lockagent_framework_adapters"
    module_path = Path(__file__).resolve().parent / "framework_adapters.py"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load framework adapters from {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.build_bundle(checkpoint)


@dataclass
class ArtifactRecord:
    path: str
    artifact_type: str
    producer: str | None = None
    consumer: str | None = None
    step_name: str | None = None
    handoff_id: str | None = None
    artifact_role: str | None = None
    required: bool = True
    schema_version: str = SCHEMA_VERSION
    sha256: str | None = None
    exists: bool = True
    timestamp: str = field(default_factory=utc_timestamp)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "artifact_type": self.artifact_type,
            "producer": self.producer,
            "consumer": self.consumer,
            "step_name": self.step_name,
            "handoff_id": self.handoff_id,
            "artifact_role": self.artifact_role,
            "required": self.required,
            "schema_version": self.schema_version,
            "sha256": self.sha256,
            "exists": self.exists,
            "timestamp": self.timestamp,
        }


class ArtifactStore:
    """Artifact index with current checksums and schema versions."""

    def __init__(self, root: Path, *, index_name: str = "artifact_store.json") -> None:
        root = root.expanduser()
        root.mkdir(parents=True, exist_ok=True)
        self.root = root.resolve()
        self.index_path = self.root / index_name
        self.records: list[ArtifactRecord] = []
        if self.index_path.exists():
            try:
                data = json.loads(self.index_path.read_text(encoding="utf-8"))
                for item in data.get("artifacts", []):
                    if isinstance(item, dict):
                        self.records.append(
                            ArtifactRecord(
                                path=str(item.get("path")),
                                artifact_type=str(item.get("artifact_type", "unknown")),
                                producer=item.get("producer"),
                                consumer=item.get("consumer"),
                                step_name=item.get("step_name"),
                                handoff_id=item.get("handoff_id"),
                                artifact_role=item.get("artifact_role"),
                                required=bool(item.get("required", True)),
                                schema_version=str(item.get("schema_version", SCHEMA_VERSION)),
                                sha256=item.get("sha256"),
                                exists=bool(item.get("exists", True)),
                                timestamp=str(item.get("timestamp", utc_timestamp())),
                            )
                        )
            except Exception:
                # Keep the runtime resilient: a corrupted index should not erase
                # the actual run artifacts.
                self.records = []

    def resolve(self, path: str | Path) -> Path:
        path = Path(path)
        if path.is_absolute():
            return path
        cwd_resolved = path.resolve()
        try:
            cwd_resolved.relative_to(self.root)
            return cwd_resolved
        except ValueError:
            pass
        return self.root / path

    def register(
        self,
        path: str | Path,
        *,
        artifact_type: str,
        producer: str | None = None,
        consumer: str | None = None,
        step_name: str | None = None,
        handoff_id: str | None = None,
        artifact_role: str | None = None,
        required: bool = True,
        schema_version: str = SCHEMA_VERSION,
    ) -> ArtifactRecord:
        resolved = self.resolve(path)
        record = ArtifactRecord(
            path=str(resolved),
            artifact_type=artifact_type,
            producer=producer,
            consumer=consumer,
            step_name=step_name,
            handoff_id=handoff_id,
            artifact_role=artifact_role,
            required=required,
            schema_version=schema_version,
            sha256=file_sha256(resolved),
            exists=resolved.exists(),
        )
        self.records = [
            existing
            for existing in self.records
            if not (
                existing.path == record.path
                and existing.artifact_role == record.artifact_role
                and existing.step_name == record.step_name
                and existing.consumer == record.consumer
            )
        ]
        self.records.append(record)
        self.flush()
        return record

    def write_json(
        self,
        path: str | Path,
        data: Any,
        *,
        artifact_type: str,
        producer: str | None = None,
        consumer: str | None = None,
        step_name: str | None = None,
        handoff_id: str | None = None,
        artifact_role: str | None = None,
        required: bool = True,
        schema_version: str = SCHEMA_VERSION,
    ) -> Path:
        resolved = self.resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(stable_json(data) + "\n", encoding="utf-8")
        self.register(
            resolved,
            artifact_type=artifact_type,
            producer=producer,
            consumer=consumer,
            step_name=step_name,
            handoff_id=handoff_id,
            artifact_role=artifact_role,
            required=required,
            schema_version=schema_version,
        )
        return resolved

    def write_text(
        self,
        path: str | Path,
        text: str,
        *,
        artifact_type: str,
        producer: str | None = None,
        consumer: str | None = None,
        step_name: str | None = None,
        handoff_id: str | None = None,
        artifact_role: str | None = None,
        required: bool = True,
        schema_version: str = SCHEMA_VERSION,
    ) -> Path:
        resolved = self.resolve(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(text, encoding="utf-8")
        self.register(
            resolved,
            artifact_type=artifact_type,
            producer=producer,
            consumer=consumer,
            step_name=step_name,
            handoff_id=handoff_id,
            artifact_role=artifact_role,
            required=required,
            schema_version=schema_version,
        )
        return resolved

    def find_by_path(self, path: str | Path) -> list[ArtifactRecord]:
        resolved = str(self.resolve(path))
        return [record for record in self.records if record.path == resolved or record.path == str(path)]

    def flush(self) -> None:
        data = {
            "schema_version": SCHEMA_VERSION,
            "artifact_root": str(self.root),
            "artifacts": [record.to_dict() for record in self.records],
        }
        self.index_path.write_text(stable_json(data) + "\n", encoding="utf-8")


class DurableStore:
    """SQLite-backed run state for durable step/event replay.

    The JSON artifacts remain the human-readable interface. This store adds a
    transactional substrate so a controller can inspect step state, event order,
    handoffs, and checkpoints without trusting one monolithic JSON file.
    """

    def __init__(self, root: Path, *, name: str = "durable_runtime.db") -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / name
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT NOT NULL,
                checkpoint_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                checkpoint_path TEXT NOT NULL,
                status TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS steps (
                run_id TEXT NOT NULL,
                step_index INTEGER NOT NULL,
                name TEXT NOT NULL,
                role TEXT NOT NULL,
                status TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, step_index)
            );
            CREATE TABLE IF NOT EXISTS events (
                event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT,
                event_type TEXT NOT NULL,
                source TEXT,
                target TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS handoffs (
                handoff_seq INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                artifacts_json TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                run_id TEXT NOT NULL,
                task_id TEXT NOT NULL,
                role TEXT NOT NULL,
                step_name TEXT NOT NULL,
                priority INTEGER NOT NULL,
                status TEXT NOT NULL,
                lease_owner TEXT,
                lease_until REAL,
                attempts INTEGER NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (run_id, task_id)
            );
            CREATE TABLE IF NOT EXISTS workers (
                worker_id TEXT PRIMARY KEY,
                roles_json TEXT NOT NULL,
                status TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                registered_at TEXT NOT NULL,
                registered_epoch REAL NOT NULL,
                last_seen TEXT NOT NULL,
                last_seen_epoch REAL NOT NULL
            );
            """
        )
        self.conn.commit()

    def record_event(self, event: dict[str, Any]) -> int:
        payload = json.dumps(event, ensure_ascii=False, sort_keys=True, default=_json_default)
        cur = self.conn.execute(
            """
            INSERT INTO events (run_id, event_type, source, target, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("run_id"),
                event.get("event_type"),
                event.get("source"),
                event.get("target"),
                payload,
                event.get("timestamp", utc_timestamp()),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_handoff(self, event: dict[str, Any]) -> int:
        artifacts = json.dumps(event.get("artifacts", []), ensure_ascii=False, sort_keys=True, default=_json_default)
        cur = self.conn.execute(
            """
            INSERT INTO handoffs (run_id, source, target, artifacts_json, reason, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                event.get("run_id"),
                str(event.get("source")),
                str(event.get("target")),
                artifacts,
                event.get("reason"),
                event.get("timestamp", utc_timestamp()),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_step(self, run_id: str, step_index: int, step: "AgentStep") -> None:
        payload = json.dumps(step.to_dict(), ensure_ascii=False, sort_keys=True, default=_json_default)
        self.conn.execute(
            """
            INSERT INTO steps (run_id, step_index, name, role, status, payload_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, step_index) DO UPDATE SET
                name=excluded.name,
                role=excluded.role,
                status=excluded.status,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (run_id, step_index, step.name, step.role, step.status, payload, utc_timestamp()),
        )
        self.conn.commit()

    def record_checkpoint(self, run_id: str, checkpoint_path: Path, status: str, payload: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO checkpoints (run_id, checkpoint_path, status, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                run_id,
                str(checkpoint_path),
                status,
                json.dumps(payload, ensure_ascii=False, sort_keys=True, default=_json_default),
                utc_timestamp(),
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def record_task(self, run_id: str, task: "AgentTask") -> None:
        self.conn.execute(
            """
            INSERT INTO tasks (
                run_id, task_id, role, step_name, priority, status,
                lease_owner, lease_until, attempts, payload_json, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, task_id) DO UPDATE SET
                role=excluded.role,
                step_name=excluded.step_name,
                priority=excluded.priority,
                status=excluded.status,
                lease_owner=excluded.lease_owner,
                lease_until=excluded.lease_until,
                attempts=excluded.attempts,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                run_id,
                task.task_id,
                task.role,
                task.step_name,
                int(task.priority),
                task.status,
                task.lease_owner,
                task.lease_until,
                int(task.attempts),
                json.dumps(task.payload, ensure_ascii=False, sort_keys=True, default=_json_default),
                utc_timestamp(),
            ),
        )
        self.conn.commit()

    def record_worker(self, worker: dict[str, Any]) -> None:
        worker_id = str(worker["worker_id"])
        roles = worker.get("roles", [])
        metadata = worker.get("metadata", {})
        now = time.time()
        self.conn.execute(
            """
            INSERT INTO workers (
                worker_id, roles_json, status, metadata_json,
                registered_at, registered_epoch, last_seen, last_seen_epoch
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(worker_id) DO UPDATE SET
                roles_json=excluded.roles_json,
                status=excluded.status,
                metadata_json=excluded.metadata_json,
                last_seen=excluded.last_seen,
                last_seen_epoch=excluded.last_seen_epoch
            """,
            (
                worker_id,
                json.dumps(roles if isinstance(roles, list) else [], ensure_ascii=False, sort_keys=True, default=_json_default),
                str(worker.get("status", "ready")),
                json.dumps(metadata if isinstance(metadata, dict) else {}, ensure_ascii=False, sort_keys=True, default=_json_default),
                str(worker.get("registered_at") or utc_timestamp()),
                float(worker.get("registered_epoch") or now),
                str(worker.get("last_seen") or utc_timestamp()),
                float(worker.get("last_seen_epoch") or now),
            ),
        )
        self.conn.commit()

    def list_tasks(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT task_id, role, step_name, priority, status, lease_owner,
                   lease_until, attempts, payload_json, updated_at
            FROM tasks WHERE run_id = ? ORDER BY priority, task_id
            """,
            (run_id,),
        ).fetchall()
        return [
            {
                "task_id": row["task_id"],
                "role": row["role"],
                "step_name": row["step_name"],
                "priority": row["priority"],
                "status": row["status"],
                "lease_owner": row["lease_owner"],
                "lease_until": row["lease_until"],
                "attempts": row["attempts"],
                "payload": json.loads(row["payload_json"]),
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def list_workers(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """
            SELECT worker_id, roles_json, status, metadata_json,
                   registered_at, registered_epoch, last_seen, last_seen_epoch
            FROM workers ORDER BY worker_id
            """
        ).fetchall()
        return [
            {
                "worker_id": row["worker_id"],
                "roles": json.loads(row["roles_json"]),
                "status": row["status"],
                "metadata": json.loads(row["metadata_json"]),
                "registered_at": row["registered_at"],
                "registered_epoch": row["registered_epoch"],
                "last_seen": row["last_seen"],
                "last_seen_epoch": row["last_seen_epoch"],
            }
            for row in rows
        ]

    def list_steps(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT step_index, name, role, status, payload_json, updated_at FROM steps WHERE run_id = ? ORDER BY step_index",
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            result.append({**payload, "step_index": row["step_index"], "updated_at": row["updated_at"]})
        return result

    def list_handoffs(self, run_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT handoff_seq, source, target, artifacts_json, reason, created_at FROM handoffs WHERE run_id = ? ORDER BY handoff_seq",
            (run_id,),
        ).fetchall()
        return [
            {
                "handoff_seq": row["handoff_seq"],
                "source": row["source"],
                "target": row["target"],
                "artifacts": json.loads(row["artifacts_json"]),
                "reason": row["reason"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def summary(self, run_id: str) -> dict[str, Any]:
        def scalar(query: str) -> int:
            row = self.conn.execute(query, (run_id,)).fetchone()
            return int(row[0] or 0)

        status_rows = self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM steps WHERE run_id = ? GROUP BY status",
            (run_id,),
        ).fetchall()
        task_status_rows = self.conn.execute(
            "SELECT status, COUNT(*) AS count FROM tasks WHERE run_id = ? GROUP BY status",
            (run_id,),
        ).fetchall()
        worker_count = self.conn.execute("SELECT COUNT(*) FROM workers").fetchone()[0]
        leased_task_count = self.conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE run_id = ? AND lease_owner IS NOT NULL",
            (run_id,),
        ).fetchone()[0]
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "db_path": str(self.path),
            "checkpoint_count": scalar("SELECT COUNT(*) FROM checkpoints WHERE run_id = ?"),
            "step_count": scalar("SELECT COUNT(*) FROM steps WHERE run_id = ?"),
            "event_count": scalar("SELECT COUNT(*) FROM events WHERE run_id = ?"),
            "handoff_count": scalar("SELECT COUNT(*) FROM handoffs WHERE run_id = ?"),
            "task_count": scalar("SELECT COUNT(*) FROM tasks WHERE run_id = ?"),
            "leased_task_count": int(leased_task_count or 0),
            "worker_count": int(worker_count or 0),
            "step_status_counts": {row["status"]: int(row["count"]) for row in status_rows},
            "task_status_counts": {row["status"]: int(row["count"]) for row in task_status_rows},
        }

    def export_summary(self, run_id: str, path: Path) -> None:
        path.write_text(stable_json(self.summary(run_id)) + "\n", encoding="utf-8")

    def close(self) -> None:
        self.conn.close()


class TypedEventBus:
    """Append-only typed message bus for role handoff and runtime events."""

    def __init__(self, root: Path, *, name: str = "typed_event_bus.jsonl", durable: DurableStore | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / name
        self.index_path = self.root / "typed_event_bus.json"
        self.durable = durable
        self.events: list[dict[str, Any]] = []
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self.events.append(event)
        self.path.touch(exist_ok=True)
        self.index_path.write_text(stable_json({"schema_version": SCHEMA_VERSION, "events": self.events}) + "\n", encoding="utf-8")

    def emit(
        self,
        *,
        run_id: str,
        event_type: str,
        source: str,
        target: str,
        payload: dict[str, Any],
        artifacts: list[str] | None = None,
        correlation_id: str | None = None,
    ) -> dict[str, Any]:
        event = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_timestamp(),
            "run_id": run_id,
            "event_type": event_type,
            "source": source,
            "target": target,
            "artifacts": artifacts or [],
            "payload": payload,
            "correlation_id": correlation_id or hashlib.sha256(f"{run_id}:{event_type}:{source}:{target}:{time.time()}".encode("utf-8")).hexdigest()[:16],
        }
        self.events.append(event)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
        self.index_path.write_text(stable_json({"schema_version": SCHEMA_VERSION, "events": self.events}) + "\n", encoding="utf-8")
        if self.durable:
            self.durable.record_event({"event_type": f"typed_bus.{event_type}", **event})
            if event_type == "handoff":
                self.durable.record_handoff(event)
        return event


class CapabilityError(PermissionError):
    pass


class CapabilityTable:
    """Capability boundary for LockAgent roles.

    ProfilerAgent has read-only host authority: it can read system state and
    write handoff artifacts, but cannot call the LLM, restart services, load
    scx, or run privileged commands. ExecutorAgent is the only role with
    host-side privileged capabilities.
    """

    def __init__(self, grants: dict[str, set[str]] | None = None) -> None:
        self.grants = grants or self.default_grants()

    @staticmethod
    def default_grants() -> dict[str, set[str]]:
        return {
            ROLE_PROFILER: {
                CAP_READ_ARTIFACT,
                CAP_WRITE_ARTIFACT,
                CAP_READ_SYSTEM_STATE,
                CAP_BUILD_PROFILE,
            },
            ROLE_POLICY: {
                CAP_READ_ARTIFACT,
                CAP_WRITE_ARTIFACT,
                CAP_CALL_LLM,
            },
            ROLE_CRITIC: {
                CAP_READ_ARTIFACT,
                CAP_WRITE_ARTIFACT,
                CAP_VALIDATE_ARTIFACT,
                CAP_EMIT_PROVENANCE,
                CAP_ATTRIBUTE_FAILURE,
            },
            ROLE_GUARD: {
                CAP_READ_ARTIFACT,
                CAP_WRITE_ARTIFACT,
                CAP_EVALUATE_POLICY,
                CAP_HUMAN_APPROVAL,
            },
            ROLE_EXECUTOR: {
                CAP_READ_ARTIFACT,
                CAP_WRITE_ARTIFACT,
                CAP_RUN_BENCHMARK,
                CAP_ROOT_CONTROL,
                CAP_LOAD_SCX,
                CAP_RESTART_SERVICE,
            },
            ROLE_CURATOR: {
                CAP_READ_ARTIFACT,
                CAP_WRITE_ARTIFACT,
                CAP_CURATE_SKILL,
                CAP_MEMORY_READ,
                CAP_MEMORY_WRITE,
            },
        }

    def check(self, role: str, capabilities: list[str] | set[str] | tuple[str, ...]) -> dict[str, Any]:
        requested = sorted(set(capabilities))
        allowed = self.grants.get(role, set())
        denied = sorted(set(requested) - allowed)
        return {
            "role": role,
            "requested": requested,
            "allowed": sorted(allowed),
            "denied": denied,
            "verdict": "allow" if not denied else "deny",
        }

    def require(self, role: str, capabilities: list[str] | set[str] | tuple[str, ...]) -> dict[str, Any]:
        verdict = self.check(role, capabilities)
        if verdict["denied"]:
            raise CapabilityError(f"{role} lacks capabilities: {verdict['denied']}")
        return verdict


@dataclass
class AgentStep:
    name: str
    role: str
    required_capabilities: list[str] = field(default_factory=list)
    input_artifacts: list[str] = field(default_factory=list)
    output_artifacts: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    start_time: str = field(default_factory=utc_timestamp)
    end_time: str | None = None
    status: str = "running"
    returncode: int | None = None
    error: str | None = None
    capability_verdict: dict[str, Any] | None = None

    def complete(self, *, status: str, returncode: int | None = None, error: str | None = None) -> None:
        self.status = status
        self.returncode = returncode
        self.error = error
        self.end_time = utc_timestamp()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "role": self.role,
            "required_capabilities": self.required_capabilities,
            "input_artifacts": self.input_artifacts,
            "output_artifacts": self.output_artifacts,
            "metadata": self.metadata,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status,
            "returncode": self.returncode,
            "error": self.error,
            "capability_verdict": self.capability_verdict,
        }


class TraceEmitter:
    """Writes JSONL trace events and a compact JSON trace index."""

    def __init__(self, root: Path, *, jsonl_name: str = "runtime_trace.jsonl") -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.root / jsonl_name
        self.json_path = self.root / "runtime_trace.json"
        self.events: list[dict[str, Any]] = []
        if self.jsonl_path.exists():
            for line in self.jsonl_path.read_text(encoding="utf-8").splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    self.events.append(event)

    def emit(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        event = {
            "schema_version": SCHEMA_VERSION,
            "timestamp": utc_timestamp(),
            "event_type": event_type,
            **payload,
        }
        self.events.append(event)
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True, default=_json_default) + "\n")
        self.json_path.write_text(stable_json({"schema_version": SCHEMA_VERSION, "events": self.events}) + "\n", encoding="utf-8")
        return event

    def span_start(self, run_id: str, step: AgentStep) -> None:
        self.emit("step_start", {"run_id": run_id, "step": step.to_dict()})

    def span_end(self, run_id: str, step: AgentStep) -> None:
        self.emit("step_end", {"run_id": run_id, "step": step.to_dict()})

    def checkpoint(self, run_id: str, path: Path) -> None:
        self.emit("checkpoint", {"run_id": run_id, "path": str(path)})

    def handoff(self, run_id: str, *, source: str, target: str, artifacts: list[str], reason: str) -> None:
        self.emit(
            "handoff",
            {
                "run_id": run_id,
                "source": source,
                "target": target,
                "artifacts": artifacts,
                "reason": reason,
            },
        )

    def export_openai_agents_schema(self, path: Path, *, run_id: str) -> None:
        spans: list[dict[str, Any]] = []
        for event in self.events:
            if event.get("run_id") != run_id:
                continue
            event_type = event.get("event_type")
            step = event.get("step", {})
            if event_type in {"step_start", "step_end"} and isinstance(step, dict):
                spans.append(
                    {
                        "trace_id": run_id,
                        "span_id": f"{step.get('name')}:{event_type}",
                        "parent_id": run_id,
                        "type": event_type,
                        "name": step.get("name"),
                        "metadata": {
                            "role": step.get("role"),
                            "status": step.get("status"),
                            "capability_verdict": step.get("capability_verdict"),
                            "input_artifacts": step.get("input_artifacts", []),
                            "output_artifacts": step.get("output_artifacts", []),
                        },
                        "timestamp": event.get("timestamp"),
                    }
                )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stable_json({"schema": "openai_agents_trace_compatible_v0", "trace_id": run_id, "spans": spans}) + "\n", encoding="utf-8")

    def export_langgraph_schema(self, path: Path, *, run: "AgentRun") -> None:
        state = {
            "schema": "langgraph_state_compatible_v0",
            "thread_id": run.run_id,
            "checkpoint": str(run.checkpoint_path),
            "values": {
                "intent": run.intent,
                "status": run.status,
                "environment_fingerprint": run.environment_fingerprint,
                "steps": [step.to_dict() for step in run.steps],
            },
            "next": run.next_resumable_step_names(),
            "metadata": {
                "graph": run.graph.to_dict(),
                "artifact_root": str(run.artifact_root),
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stable_json(state) + "\n", encoding="utf-8")

    def export_autogen_schema(self, path: Path, *, run: "AgentRun") -> None:
        messages: list[dict[str, Any]] = []
        for event in self.events:
            if event.get("run_id") != run.run_id:
                continue
            if event.get("event_type") == "handoff":
                messages.append(
                    {
                        "type": "handoff_message",
                        "source": event.get("source"),
                        "target": event.get("target"),
                        "content": {
                            "artifacts": event.get("artifacts", []),
                            "reason": event.get("reason"),
                        },
                        "timestamp": event.get("timestamp"),
                    }
                )
            elif event.get("event_type") in {"step_start", "step_end"}:
                step = event.get("step", {})
                if isinstance(step, dict):
                    messages.append(
                        {
                            "type": event.get("event_type"),
                            "source": step.get("role"),
                            "target": "runtime",
                            "content": step,
                            "timestamp": event.get("timestamp"),
                        }
                    )
        data = {
            "schema": "autogen_core_trace_compatible_v0",
            "runtime": {
                "run_id": run.run_id,
                "status": run.status,
                "agent_ids": run.graph.nodes,
            },
            "messages": messages,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stable_json(data) + "\n", encoding="utf-8")

    def export_crewai_schema(self, path: Path, *, run: "AgentRun") -> None:
        tasks = []
        for step in run.steps:
            tasks.append(
                {
                    "task_id": step.metadata.get("scheduler_task_id", step.name),
                    "description": step.name,
                    "agent": step.role,
                    "status": step.status,
                    "inputs": step.input_artifacts,
                    "outputs": step.output_artifacts,
                    "metadata": {
                        "capabilities": step.required_capabilities,
                        "returncode": step.returncode,
                        "error": step.error,
                    },
                }
            )
        data = {
            "schema": "crewai_trace_compatible_v0",
            "crew": {
                "name": "LockAgent",
                "process": "sequential_typed_handoff",
                "agents": run.graph.nodes,
            },
            "tasks": tasks,
            "memory": str(run.memory.path),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stable_json(data) + "\n", encoding="utf-8")

    def export_schema_alignment_report(self, path: Path, *, run: "AgentRun") -> None:
        """Validate that exported traces preserve mainstream agent-runtime concepts."""

        root = path.parent

        def load(name: str) -> dict[str, Any]:
            candidate = root / name
            if not candidate.exists():
                return {}
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                return {}
            return data if isinstance(data, dict) else {}

        openai_trace = load("openai_agents_trace.json")
        langgraph_state = load("langgraph_state.json")
        autogen_trace = load("autogen_trace.json")
        crewai_trace = load("crewai_trace.json")
        adapter_bundle = load("framework_adapter_bundle.json")
        handoff_events = [
            event
            for event in self.events
            if event.get("run_id") == run.run_id and event.get("event_type") == "handoff"
        ]

        checks: dict[str, dict[str, Any]] = {}
        openai_spans = openai_trace.get("spans", [])
        checks["openai_agents"] = {
            "passed": (
                openai_trace.get("schema") == "openai_agents_trace_compatible_v0"
                and openai_trace.get("trace_id") == run.run_id
                and isinstance(openai_spans, list)
                and all(isinstance(span, dict) and span.get("name") and isinstance(span.get("metadata"), dict) for span in openai_spans)
            ),
            "span_count": len(openai_spans) if isinstance(openai_spans, list) else 0,
            "concepts": ["trace_id", "spans", "span_metadata", "tool_step_inputs_outputs"],
        }

        langgraph_values = langgraph_state.get("values", {})
        langgraph_graph = langgraph_state.get("metadata", {}).get("graph", {})
        checks["langgraph"] = {
            "passed": (
                langgraph_state.get("schema") == "langgraph_state_compatible_v0"
                and langgraph_state.get("thread_id") == run.run_id
                and bool(langgraph_state.get("checkpoint"))
                and isinstance(langgraph_values.get("steps"), list)
                and isinstance(langgraph_state.get("next"), list)
                and isinstance(langgraph_graph.get("nodes"), list)
                and isinstance(langgraph_graph.get("edges"), list)
            ),
            "step_count": len(langgraph_values.get("steps", [])) if isinstance(langgraph_values.get("steps"), list) else 0,
            "graph_nodes": len(langgraph_graph.get("nodes", [])) if isinstance(langgraph_graph.get("nodes"), list) else 0,
            "graph_edges": len(langgraph_graph.get("edges", [])) if isinstance(langgraph_graph.get("edges"), list) else 0,
            "concepts": ["thread_id", "checkpoint", "state_values", "next", "state_graph"],
        }

        autogen_messages = autogen_trace.get("messages", [])
        checks["autogen_core"] = {
            "passed": (
                autogen_trace.get("schema") == "autogen_core_trace_compatible_v0"
                and isinstance(autogen_trace.get("runtime"), dict)
                and isinstance(autogen_trace.get("runtime", {}).get("agent_ids"), list)
                and isinstance(autogen_messages, list)
                and any(isinstance(message, dict) and message.get("type") == "handoff_message" for message in autogen_messages)
            ),
            "message_count": len(autogen_messages) if isinstance(autogen_messages, list) else 0,
            "handoff_message_count": sum(1 for message in autogen_messages if isinstance(message, dict) and message.get("type") == "handoff_message"),
            "concepts": ["agent_ids", "messages", "handoff_message", "source_target"],
        }

        crew_tasks = crewai_trace.get("tasks", [])
        crew = crewai_trace.get("crew", {})
        checks["crewai"] = {
            "passed": (
                crewai_trace.get("schema") == "crewai_trace_compatible_v0"
                and isinstance(crew, dict)
                and crew.get("name") == "LockAgent"
                and isinstance(crew.get("agents"), list)
                and isinstance(crew_tasks, list)
                and all(isinstance(task, dict) and task.get("agent") and "inputs" in task and "outputs" in task for task in crew_tasks)
            ),
            "task_count": len(crew_tasks) if isinstance(crew_tasks, list) else 0,
            "agent_count": len(crew.get("agents", [])) if isinstance(crew.get("agents"), list) else 0,
            "concepts": ["crew", "agents", "tasks", "task_inputs_outputs", "memory"],
        }

        checks["typed_handoff"] = {
            "passed": bool(handoff_events)
            and all(
                isinstance(event.get("source"), str)
                and isinstance(event.get("target"), str)
                and isinstance(event.get("artifacts"), list)
                and event.get("reason")
                for event in handoff_events
            ),
            "handoff_event_count": len(handoff_events),
            "concepts": ["source", "target", "artifacts", "handoff_reason"],
        }

        durable_summary_path = root / "durable_runtime_summary.json"
        durable_summary = {}
        if durable_summary_path.exists():
            try:
                durable_summary = json.loads(durable_summary_path.read_text(encoding="utf-8"))
            except Exception:
                durable_summary = {}
        typed_bus_path = root / "typed_event_bus.json"
        typed_bus = {}
        if typed_bus_path.exists():
            try:
                typed_bus = json.loads(typed_bus_path.read_text(encoding="utf-8"))
            except Exception:
                typed_bus = {}
        typed_events = typed_bus.get("events", [])
        checks["durable_execution"] = {
            "passed": (
                bool((root / "durable_runtime.db").exists())
                and durable_summary.get("checkpoint_count", 0) > 0
                and durable_summary.get("step_count", 0) >= len(run.steps)
            ),
            "checkpoint_count": durable_summary.get("checkpoint_count", 0),
            "step_count": durable_summary.get("step_count", 0),
            "event_count": durable_summary.get("event_count", 0),
            "concepts": ["sqlite_checkpoint_store", "step_state_table", "event_log", "resume_state"],
        }
        checks["typed_event_bus"] = {
            "passed": (
                typed_bus.get("schema_version") == SCHEMA_VERSION
                and isinstance(typed_events, list)
                and any(isinstance(event, dict) and event.get("event_type") == "handoff" for event in typed_events)
                and all(
                    isinstance(event, dict)
                    and event.get("source")
                    and event.get("target")
                    and isinstance(event.get("payload"), dict)
                    for event in typed_events
                )
            ),
            "event_count": len(typed_events) if isinstance(typed_events, list) else 0,
            "handoff_count": sum(1 for event in typed_events if isinstance(event, dict) and event.get("event_type") == "handoff") if isinstance(typed_events, list) else 0,
            "concepts": ["typed_message", "source_target", "payload_schema", "correlation_id"],
        }
        resume_plan = load("resume_plan.json")
        resume_graph = resume_plan.get("resume_graph", {}) if isinstance(resume_plan, dict) else {}
        resume_nodes = resume_graph.get("nodes", []) if isinstance(resume_graph, dict) else []
        resume_edges = resume_graph.get("edges", []) if isinstance(resume_graph, dict) else []
        has_resumable_work = bool(run.next_resumable_step_names()) or bool(run.scheduler.queued_command_tasks())
        resume_graph_present = bool(resume_plan)
        checks["resume_graph"] = {
            "passed": (
                (
                    resume_graph_present
                    and isinstance(resume_nodes, list)
                    and isinstance(resume_edges, list)
                    and any(isinstance(node, dict) and node.get("type") in {"replay_action", "queued_task_replay"} for node in resume_nodes)
                    and any(isinstance(edge, dict) and edge.get("type") in {"resume_dependency", "artifact_dependency"} for edge in resume_edges)
                )
                or (not resume_graph_present and not has_resumable_work)
            ),
            "required": has_resumable_work,
            "present": resume_graph_present,
            "node_count": len(resume_nodes) if isinstance(resume_nodes, list) else 0,
            "edge_count": len(resume_edges) if isinstance(resume_edges, list) else 0,
            "replayable_step_count": resume_graph.get("replayable_step_count", 0) if isinstance(resume_graph, dict) else 0,
            "queued_task_replay_count": resume_graph.get("queued_task_replay_count", 0) if isinstance(resume_graph, dict) else 0,
            "concepts": ["checkpoint_step", "replay_action", "task_replay", "artifact_dependency", "resume_dependency"],
        }
        adapter_coverage = adapter_bundle.get("coverage", {}) if isinstance(adapter_bundle, dict) else {}
        checks["framework_adapter_bundle"] = {
            "passed": (
                adapter_bundle.get("schema_version") == "lockagent.framework_adapters.v0.1"
                and adapter_coverage.get("status") == "aligned"
                and adapter_coverage.get("coverage_score", 0) >= 1.0
                and all(key in adapter_bundle for key in ("openai_agents", "langgraph", "autogen_core", "crewai"))
            ),
            "coverage_score": adapter_coverage.get("coverage_score", 0),
            "status": adapter_coverage.get("status"),
            "concepts": ["adapter_bundle", "schema_coverage", "framework_mapping", "handoff_mapping"],
        }

        passed = sum(1 for check in checks.values() if check["passed"])
        report = {
            "schema_version": "lockagent.trace_schema_alignment.v0.1",
            "run_id": run.run_id,
            "timestamp": utc_timestamp(),
            "status": "aligned" if passed == len(checks) else "partial",
            "alignment_score": round(passed / max(len(checks), 1), 3),
            "checks": checks,
            "exported_artifacts": {
                "openai_agents_trace": str(root / "openai_agents_trace.json"),
                "langgraph_state": str(root / "langgraph_state.json"),
                "autogen_trace": str(root / "autogen_trace.json"),
                "crewai_trace": str(root / "crewai_trace.json"),
                "framework_adapter_bundle": str(root / "framework_adapter_bundle.json"),
                "durable_runtime_db": str(root / "durable_runtime.db"),
                "typed_event_bus": str(root / "typed_event_bus.json"),
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(stable_json(report) + "\n", encoding="utf-8")


PolicyCheck = Callable[[dict[str, Any]], tuple[bool, str | None]]


@dataclass
class PolicyRule:
    name: str
    severity: str
    category: str
    description: str
    evidence_source: str
    remediation: str
    failure_category: str | None
    owner_role: str
    check: PolicyCheck

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        passed, reason = self.check(context)
        return {
            "name": self.name,
            "severity": self.severity,
            "category": self.category,
            "passed": passed,
            "reason": reason,
            "description": self.description,
            "evidence_source": self.evidence_source,
            "remediation": self.remediation,
            "failure_category": self.failure_category,
            "owner_role": self.owner_role,
        }


class PolicyEngine:
    """Declarative runtime policy evaluator for scheduler actions."""

    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self.rules = rules or self.default_rules()

    @staticmethod
    def _decision(context: dict[str, Any]) -> dict[str, Any] | None:
        decision = context.get("decision")
        return decision if isinstance(decision, dict) else None

    @staticmethod
    def _phase(context: dict[str, Any]) -> str:
        return str(context.get("phase", "preflight"))

    @staticmethod
    def default_rules() -> list[PolicyRule]:
        allowed = {
            "default",
            "scx_simple",
            "scx_lockboost",
            "scx_lockcoop",
            "scx_lockcoop",
            "scx_hybrid",
            "scx_toponest",
            "scx_cfl",
            "scx_lcas",
        }

        def rule(
            name: str,
            severity: str,
            category: str,
            description: str,
            evidence_source: str,
            remediation: str,
            failure_category: str | None,
            owner_role: str,
            check: PolicyCheck,
        ) -> PolicyRule:
            return PolicyRule(
                name=name,
                severity=severity,
                category=category,
                description=description,
                evidence_source=evidence_source,
                remediation=remediation,
                failure_category=failure_category,
                owner_role=owner_role,
                check=check,
            )

        def decision_exists(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            return (PolicyEngine._decision(context) is not None, "missing_decision")

        def scheduler_allowlisted(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            scheduler = decision.get("scheduler") if decision else None
            return (scheduler in allowed, f"scheduler_not_allowlisted:{scheduler}")

        def decision_schema_valid(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            errors = context.get("decision_schema_errors") or []
            return (not errors, f"invalid_decision_schema:{';'.join(str(item) for item in errors)}")

        def confidence_in_range(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            confidence = decision.get("confidence") if decision else None
            return (isinstance(confidence, (int, float)) and 0.0 <= float(confidence) <= 1.0, f"confidence_out_of_range:{confidence}")

        def evidence_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            evidence = decision.get("evidence_used") if decision else None
            return (isinstance(evidence, list) and bool(evidence), "missing_evidence_used")

        def evidence_resolves_to_profile_or_skill(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            evidence = decision.get("evidence_used") if decision else []
            if not isinstance(evidence, list) or not evidence:
                return True, None
            profile_exists = bool(context.get("profile_exists"))
            ledger_exists = bool(context.get("ledger_exists"))
            skill_names = {
                str(card.get("name"))
                for card in context.get("skill_cards", [])
                if isinstance(card, dict) and card.get("name")
            }
            experience_card_ids = {
                str(item)
                for item in context.get("experience_card_ids", []) or []
                if item
            }
            unresolved: list[str] = []
            for item in evidence:
                ref = str(item)
                if ref in {"online_profile_window", "default_profile_window", "profiling_window"}:
                    continue
                if ref.startswith("profile.") and profile_exists:
                    continue
                if ref.startswith("ledger.") and ledger_exists:
                    continue
                if ref.startswith("skill."):
                    skill_ref = ref.split(".", 1)[1]
                    if skill_ref in skill_names:
                        continue
                if ref in skill_names or ref in experience_card_ids:
                    continue
                if ref.startswith("experience.") or ref.startswith("experience_cards."):
                    experience_ref = ref.split(".", 1)[1]
                    if experience_ref in experience_card_ids:
                        continue
                if ref.startswith("memory.") or ref.startswith("long_term_memory."):
                    continue
                unresolved.append(ref)
            return (not unresolved, "unresolved_evidence:" + ",".join(unresolved))

        def no_oracle_leakage(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("profile_contains_oracle_best"):
                return False, "profile_contains_oracle_best"
            decision = PolicyEngine._decision(context)
            evidence = decision.get("evidence_used") if decision else []
            if isinstance(evidence, list):
                leaked = [str(item) for item in evidence if "oracle" in str(item).lower() or "best-fixed" in str(item).lower()]
                if leaked:
                    return False, "decision_uses_oracle_evidence:" + ",".join(leaked)
            return True, None

        def validation_plan_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            plan = decision.get("validation_plan") if decision else None
            return (isinstance(plan, list) and bool(plan), "missing_validation_plan")

        def risks_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            risks = decision.get("risks") if decision else None
            return (isinstance(risks, list) and bool(risks), "missing_risks")

        def rollback_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            rollback = decision.get("rollback_conditions") if decision else None
            return (isinstance(rollback, list) and bool(rollback), "missing_rollback_conditions")

        def ledger_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            return (bool(context.get("ledger_exists")), "missing_state_ledger")

        def profile_required_for_non_default(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            scheduler = decision.get("scheduler") if decision else None
            if scheduler and scheduler != "default":
                return (bool(context.get("profile_exists")), "non_default_scheduler_requires_profile")
            return True, None

        def candidate_scheduler_available(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            scheduler = decision.get("scheduler") if decision else None
            candidates = context.get("profile_candidate_schedulers") or []
            if scheduler and candidates:
                return (scheduler in candidates, f"scheduler_not_in_profile_candidates:{scheduler}")
            return True, None

        def lock_signal_required_for_scx(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            scheduler = decision.get("scheduler") if decision else None
            if not scheduler or scheduler == "default":
                return True, None
            signals = context.get("profile_signals") or {}
            if not isinstance(signals, dict):
                return False, "non_default_scheduler_requires_lock_signals"
            lock_keys = ("lock_wait", "wakeup_to_run", "hot_lock_fanin", "owner_preempt_score", "handoff_distance", "cross_numa_handoff")
            has_signal = any(key in signals and signals.get(key) not in (None, 0, 0.0, "", [], {}) for key in lock_keys)
            return (has_signal, "non_default_scheduler_requires_lock_signals")

        def database_status_known_for_strict_actions(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            scheduler = decision.get("scheduler") if decision else None
            status = context.get("ledger_database_status")
            if context.get("require_environment_compatibility") or context.get("intent") == "reproduction":
                return (status == "ok", f"database_state_not_ok:{status}")
            if context.get("dry_run"):
                return True, None
            if context.get("requires_executor") and scheduler not in {None, "default"}:
                return (status in {None, "ok"}, f"database_state_not_ok:{status}")
            return True, None

        def benchmark_protocol_known_for_comparison(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("intent") == "reproduction" or context.get("require_environment_compatibility"):
                protocol = context.get("ledger_benchmark_protocol")
                return (bool(protocol), "missing_benchmark_restart_protocol")
            return True, None

        def reproduction_intent_warning(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("intent") == "reproduction":
                return False, "reproduction_intent_requires_environment_compatibility"
            return True, None

        def database_state_known_warning(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            status = context.get("ledger_database_status")
            if status != "ok":
                return False, f"database_state_unknown:{status}"
            return True, None

        def dry_run_records_no_side_effect(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            scheduler = decision.get("scheduler") if decision else None
            if context.get("dry_run") and scheduler not in (None, "default"):
                return False, f"dry_run_no_real_scx_load:{scheduler}"
            return True, None

        def manifest_required_for_executor(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("requires_executor"):
                return (bool(context.get("manifest_exists")), "missing_execution_manifest")
            return True, None

        def manifest_scheduler_matches_decision(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight" or not context.get("manifest_exists"):
                return True, None
            decision = PolicyEngine._decision(context)
            manifest_scheduler = context.get("manifest_scheduler")
            scheduler = decision.get("scheduler") if decision else None
            if manifest_scheduler in (None, ""):
                return True, None
            return (manifest_scheduler == scheduler, f"manifest_scheduler_mismatch:{manifest_scheduler}!={scheduler}")

        def human_approval_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("requires_human_approval"):
                return (bool(context.get("human_approved")), "human_approval_required")
            return True, None

        def reproduction_requires_compatibility(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("intent") == "reproduction":
                return (bool(context.get("environment_compatible")), "environment_compatibility_required")
            return True, None

        def systemic_environment_gate(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("require_environment_compatibility"):
                return (bool(context.get("environment_compatible", True)), "systemic_environment_compatibility_required")
            return True, None

        def scheduler_not_disabled(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            decision = PolicyEngine._decision(context)
            scheduler = decision.get("scheduler") if decision else None
            disabled = set(context.get("scheduler_disablelist") or [])
            return (scheduler not in disabled, f"scheduler_disabled_by_systemic_policy:{scheduler}")

        def strict_critic_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if context.get("require_strict_critic"):
                return (context.get("critic_verdict") == "allow", "strict_critic_preflight_required")
            return True, None

        def upstream_policy_preflight_allows(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "preflight":
                return True, None
            if not context.get("upstream_policy_required"):
                return True, None
            if not context.get("upstream_policy_exists"):
                return False, "missing_runtime_policy_preflight"
            verdict = context.get("upstream_policy_verdict")
            return (verdict == "allow", f"runtime_policy_preflight_not_allow:{verdict}")

        def status_required(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "postflight":
                return True, None
            return (bool(context.get("status_exists")), "missing_execution_status")

        def final_sched_ext_disabled(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "postflight" or not context.get("status_exists"):
                return True, None
            state = context.get("final_sched_ext_state")
            return (state == "disabled", f"sched_ext_not_disabled:{state}")

        def benchmark_log_preserved(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "postflight" or not context.get("benchmark_log_expected"):
                return True, None
            return (bool(context.get("benchmark_log_exists")), "missing_benchmark_log")

        def no_residual_scx(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "postflight":
                return True, None
            residual = context.get("scx_residual_processes") or []
            return (not residual, "scx_residual_process")

        def executor_returncode_warning(context: dict[str, Any]) -> tuple[bool, str | None]:
            if PolicyEngine._phase(context) != "postflight" or not context.get("status_exists"):
                return True, None
            return (context.get("executor_returncode") == 0, "executor_returncode_nonzero")

        return [
            rule("decision_exists", "block", "schema", "A policy decision artifact must exist.", "decision.json", "Generate a decision artifact before policy evaluation.", "policy_reasoning_failure", ROLE_CRITIC, decision_exists),
            rule("decision_schema_valid", "block", "schema", "Decision must satisfy the declared schema.", "decision.json", "Regenerate the LLM decision with the required schema.", "policy_reasoning_failure", ROLE_CRITIC, decision_schema_valid),
            rule("scheduler_allowlisted", "block", "scheduler_allowlist", "Scheduler must belong to the runtime allowlist.", "decision.scheduler", "Choose one scheduler from the LockAgent allowlist.", "policy_reasoning_failure", ROLE_GUARD, scheduler_allowlisted),
            rule("scheduler_not_disabled", "block", "scheduler_allowlist", "Scheduler must not be disabled by systemic policy.", "systemic_overrides.scheduler_disablelist", "Select another scheduler or clear the disablelist with evidence.", "guard_blocked", ROLE_GUARD, scheduler_not_disabled),
            rule("confidence_in_range", "block", "schema", "Decision confidence must be in [0, 1].", "decision.confidence", "Regenerate the decision with bounded confidence.", "policy_reasoning_failure", ROLE_CRITIC, confidence_in_range),
            rule("evidence_required", "block", "profile_evidence", "Decision must cite evidence.", "decision.evidence_used", "Cite profile, ledger, skill, memory, or experience evidence.", "policy_reasoning_failure", ROLE_CRITIC, evidence_required),
            rule("evidence_resolves_to_profile_or_skill", "block", "profile_evidence", "Evidence refs must resolve to profile, ledger, skill, memory, or experience sources.", "decision.evidence_used", "Use resolvable evidence IDs from profile, ledger, skills, memory, or experience cards.", "policy_reasoning_failure", ROLE_CRITIC, evidence_resolves_to_profile_or_skill),
            rule("no_oracle_leakage", "block", "oracle_leakage", "Policy input must not contain oracle-best or best-fixed leakage.", "profile.data_provenance/decision.evidence_used", "Rebuild profile and prompt without oracle/best-fixed evaluation fields.", "comparison_invalid", ROLE_CRITIC, no_oracle_leakage),
            rule("validation_plan_required", "block", "schema", "Decision must include validation plan.", "decision.validation_plan", "Add a validation plan before executor authority.", "policy_reasoning_failure", ROLE_CRITIC, validation_plan_required),
            rule("risks_required", "block", "schema", "Decision must include risks.", "decision.risks", "Add risk analysis before executor authority.", "policy_reasoning_failure", ROLE_CRITIC, risks_required),
            rule("rollback_required", "block", "rollback", "Rollback conditions are mandatory before executor authority.", "decision.rollback_conditions", "Provide explicit rollback conditions.", "rollback_failure", ROLE_CRITIC, rollback_required),
            rule("ledger_required", "block", "environment_compatibility", "State ledger is mandatory before executor authority.", "state_ledger.json", "Collect state ledger before evaluating privileged action.", "environment_mismatch", ROLE_GUARD, ledger_required),
            rule("profile_required_for_non_default", "block", "profile_evidence", "Non-default scheduler actions require a profile artifact.", "profile.json", "Build a workload profile before non-default scheduler action.", "profile_failure", ROLE_GUARD, profile_required_for_non_default),
            rule("candidate_scheduler_available", "block", "scheduler_allowlist", "Chosen scheduler must be present in profile candidates when declared.", "profile.candidate_schedulers", "Choose a scheduler from profile candidates or rebuild profile.", "policy_reasoning_failure", ROLE_GUARD, candidate_scheduler_available),
            rule("lock_signal_required_for_scx", "block", "profile_evidence", "Non-default schedulers require lock-contention evidence.", "profile.signals", "Collect lock/perf signals or fall back to default.", "profile_failure", ROLE_GUARD, lock_signal_required_for_scx),
            rule("database_status_known_for_strict_actions", "block", "environment_compatibility", "Strict executor/comparison actions require known database state.", "state_ledger.database", "Recollect database state or disable strict comparison.", "environment_mismatch", ROLE_GUARD, database_status_known_for_strict_actions),
            rule("benchmark_protocol_known_for_comparison", "block", "environment_compatibility", "Comparison/reproduction requires benchmark protocol provenance.", "state_ledger.benchmark", "Record benchmark restart/warmup/run protocol.", "comparison_invalid", ROLE_GUARD, benchmark_protocol_known_for_comparison),
            rule("reproduction_intent_warning", "warn", "environment_compatibility", "Reproduction intent should be paired with an explicit compatibility check.", "runner.intent", "Run environment compatibility check before making a reproduction claim.", "comparison_invalid", ROLE_GUARD, reproduction_intent_warning),
            rule("database_state_known_warning", "warn", "environment_compatibility", "Database state should be known for auditable scheduler experiments.", "state_ledger.database", "Recollect database state before paper-grade execution.", "environment_mismatch", ROLE_GUARD, database_state_known_warning),
            rule("manifest_required_for_executor", "block", "manifest_consistency", "Executor authority requires an execution manifest.", "execution/manifest.json", "Materialize execution manifest before privileged execution.", "executor_failure", ROLE_GUARD, manifest_required_for_executor),
            rule("manifest_scheduler_matches_decision", "block", "manifest_consistency", "Execution manifest must match decision scheduler when declared.", "execution/manifest.json", "Regenerate manifest from the current decision.", "executor_failure", ROLE_GUARD, manifest_scheduler_matches_decision),
            rule("human_approval_required", "block", "runtime_authorization", "High-risk actions require HITL approval.", "human_gate.json", "Record human approval before proceeding.", "guard_blocked", ROLE_GUARD, human_approval_required),
            rule("reproduction_requires_compatibility", "block", "environment_compatibility", "Reproduction mode requires environment compatibility.", "environment_compatibility.json", "Run compatibility check or avoid reproduction claim.", "comparison_invalid", ROLE_GUARD, reproduction_requires_compatibility),
            rule("systemic_environment_gate", "block", "environment_compatibility", "Systemic policy may require environment compatibility.", "systemic_overrides.json", "Satisfy environment compatibility gate before execution.", "environment_mismatch", ROLE_GUARD, systemic_environment_gate),
            rule("strict_critic_required", "block", "runtime_authorization", "Systemic policy may require strict Critic preflight.", "critic_preflight.json", "Run CriticAgent and obtain allow verdict.", "policy_reasoning_failure", ROLE_GUARD, strict_critic_required),
            rule("upstream_policy_preflight_allows", "block", "runtime_authorization", "Executor must consume an allowing runtime policy preflight artifact when one is supplied.", "runtime_policy_preflight.json", "Re-run runtime policy preflight or pass the current allowing artifact to ExecutorAgent.", "guard_blocked", ROLE_GUARD, upstream_policy_preflight_allows),
            rule("status_required", "block", "postflight_cleanup", "Postflight requires execution status.", "execution/status.json", "Write executor status before postflight validation.", "executor_failure", ROLE_GUARD, status_required),
            rule("final_sched_ext_disabled", "block", "postflight_cleanup", "Postflight requires sched_ext to be disabled.", "execution/status.json", "Unload scheduler and verify sched_ext disabled.", "rollback_failure", ROLE_GUARD, final_sched_ext_disabled),
            rule("benchmark_log_preserved", "warn", "postflight_cleanup", "Benchmark log should be preserved.", "execution/benchmark.log", "Preserve benchmark log for audit and metric parsing.", "benchmark_failure", ROLE_GUARD, benchmark_log_preserved),
            rule("no_residual_scx", "block", "postflight_cleanup", "No scx scheduler process may remain after execution.", "process_table", "Terminate residual scx scheduler processes.", "rollback_failure", ROLE_GUARD, no_residual_scx),
            rule("executor_returncode_warning", "warn", "postflight_cleanup", "Executor returncode should be zero.", "execution/status.json", "Inspect executor/benchmark logs and failure attribution.", "executor_failure", ROLE_GUARD, executor_returncode_warning),
            rule("dry_run_records_no_side_effect", "warn", "runtime_authorization", "Dry-run may record intent but must not perform scx side effects.", "runner.dry_run", "Keep executor in dry-run mode or choose default.", "guard_blocked", ROLE_GUARD, dry_run_records_no_side_effect),
        ]

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        checks = [rule.evaluate(context) for rule in self.rules]
        blocking = [check for check in checks if not check["passed"] and check["severity"] == "block"]
        warnings = [check for check in checks if not check["passed"] and check["severity"] == "warn"]
        return {
            "schema_version": SCHEMA_VERSION,
            "verdict": "allow" if not blocking else "block",
            "checks": checks,
            "blocking_reasons": [check["reason"] for check in blocking],
            "warnings": [check["reason"] for check in warnings],
        }


@dataclass
class ExecutionGraph:
    nodes: list[str]
    edges: list[tuple[str, str]]
    step_nodes: list[dict[str, Any]] = field(default_factory=list)
    step_edges: list[dict[str, Any]] = field(default_factory=list)

    @staticmethod
    def default() -> "ExecutionGraph":
        step_nodes, step_edges = ExecutionGraph.step_template_records(
            ExecutionGraph.default_step_template()
        )
        return ExecutionGraph(
            nodes=[ROLE_PROFILER, ROLE_POLICY, ROLE_CRITIC, ROLE_GUARD, ROLE_EXECUTOR, ROLE_CURATOR],
            edges=[
                (ROLE_PROFILER, ROLE_POLICY),
                (ROLE_POLICY, ROLE_CRITIC),
                (ROLE_CRITIC, ROLE_GUARD),
                (ROLE_GUARD, ROLE_EXECUTOR),
                (ROLE_EXECUTOR, ROLE_CRITIC),
                (ROLE_CRITIC, ROLE_CURATOR),
            ],
            step_nodes=step_nodes,
            step_edges=step_edges,
        )

    @staticmethod
    def default_step_template() -> list[tuple[str, str]]:
        return [
            ("state_ledger", ROLE_PROFILER),
            ("restart_mariadb_for_profile", ROLE_EXECUTOR),
            ("default_profile_window", ROLE_EXECUTOR),
            ("profile_builder", ROLE_PROFILER),
            ("llm_policy_agent", ROLE_POLICY),
            ("critic_preflight", ROLE_CRITIC),
            ("runtime_policy_preflight", ROLE_GUARD),
            ("restart_mariadb_for_execution", ROLE_EXECUTOR),
            ("scx_executor", ROLE_EXECUTOR),
            ("critic_postflight", ROLE_CRITIC),
            ("build_provenance", ROLE_CRITIC),
            ("skill_curator", ROLE_CURATOR),
            ("self_evolution_planner", ROLE_CURATOR),
            ("global_memory_append", ROLE_CURATOR),
            ("build_final_provenance", ROLE_CRITIC),
            ("causal_attribution", ROLE_CRITIC),
            ("counterfactual_planner", ROLE_CRITIC),
            ("counterfactual_execute_validation", ROLE_CURATOR),
            ("skill_evolution_application", ROLE_CURATOR),
        ]

    @staticmethod
    def step_template_records(steps: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        step_nodes = [
            {"id": f"step_template.{index}.{name}", "name": name, "role": role, "index": index}
            for index, (name, role) in enumerate(steps)
        ]
        step_edges = [
            {
                "from": step_nodes[index]["id"],
                "to": step_nodes[index + 1]["id"],
                "type": "control_dependency",
            }
            for index in range(len(step_nodes) - 1)
        ]
        return step_nodes, step_edges

    @staticmethod
    def from_roles(roles: list[str]) -> "ExecutionGraph":
        edges = [(roles[index], roles[index + 1]) for index in range(len(roles) - 1)]
        step_nodes, step_edges = ExecutionGraph.step_template_records(
            [(f"role_step_{index}_{role}", role) for index, role in enumerate(roles)]
        )
        return ExecutionGraph(nodes=roles, edges=edges, step_nodes=step_nodes, step_edges=step_edges)

    @staticmethod
    def from_step_template(steps: list[tuple[str, str]]) -> "ExecutionGraph":
        roles = [role for _, role in steps]
        unique_roles = list(dict.fromkeys(roles))
        role_edges: list[tuple[str, str]] = []
        for source, target in zip(roles, roles[1:]):
            edge = (source, target)
            if edge not in role_edges:
                role_edges.append(edge)
        step_nodes, step_edges = ExecutionGraph.step_template_records(steps)
        return ExecutionGraph(
            nodes=unique_roles,
            edges=role_edges,
            step_nodes=step_nodes,
            step_edges=step_edges,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": self.nodes,
            "edges": [{"from": source, "to": target} for source, target in self.edges],
            "step_nodes": self.step_nodes,
            "step_edges": self.step_edges,
            "role_sequence": [node.get("role") for node in self.step_nodes if isinstance(node, dict) and node.get("role")],
        }

    def successors(self, role: str) -> list[str]:
        return [target for source, target in self.edges if source == role]

    def predecessors(self, role: str) -> list[str]:
        return [source for source, target in self.edges if target == role]

    def step_node_for(self, name: str, role: str) -> dict[str, Any] | None:
        for node in self.step_nodes:
            if not isinstance(node, dict):
                continue
            if node.get("name") == name and node.get("role") == role:
                return node
        return None


class MemoryStore:
    """Small persistent memory store for run-level and system-level evidence."""

    def __init__(self, root: Path, *, name: str = "memory_store.json") -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / name
        self.items: list[dict[str, Any]] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.items = [item for item in data.get("items", []) if isinstance(item, dict)]
            except Exception:
                self.items = []
        self.flush()

    def add(self, *, namespace: str, key: str, value: Any, source: str | None = None) -> dict[str, Any]:
        item = {
            "namespace": namespace,
            "key": key,
            "value": value,
            "source": source,
            "timestamp": utc_timestamp(),
        }
        self.items.append(item)
        self.flush()
        return item

    def query(self, *, namespace: str | None = None, key: str | None = None) -> list[dict[str, Any]]:
        result = self.items
        if namespace is not None:
            result = [item for item in result if item.get("namespace") == namespace]
        if key is not None:
            result = [item for item in result if item.get("key") == key]
        return result

    def flush(self) -> None:
        self.path.write_text(stable_json({"schema_version": SCHEMA_VERSION, "items": self.items}) + "\n", encoding="utf-8")


@dataclass(order=True)
class AgentTask:
    priority: int
    task_id: str = field(compare=False)
    role: str = field(compare=False)
    step_name: str = field(compare=False)
    payload: dict[str, Any] = field(default_factory=dict, compare=False)
    status: str = field(default="queued", compare=False)
    lease_owner: str | None = field(default=None, compare=False)
    lease_until: float | None = field(default=None, compare=False)
    attempts: int = field(default=0, compare=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "priority": self.priority,
            "task_id": self.task_id,
            "role": self.role,
            "step_name": self.step_name,
            "payload": self.payload,
            "status": self.status,
            "lease_owner": self.lease_owner,
            "lease_until": self.lease_until,
            "attempts": self.attempts,
        }


class AgentScheduler:
    """Priority work queue for concurrent/distributed agent execution."""

    def __init__(self, root: Path, *, name: str = "agent_scheduler.json", durable: DurableStore | None = None, run_id: str | None = None) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / name
        self.durable = durable
        self.run_id = run_id
        self._queue: list[AgentTask] = []
        self.history: list[dict[str, Any]] = []
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.history = [item for item in data.get("history", []) if isinstance(item, dict)]
                for item in data.get("tasks", []):
                    if isinstance(item, dict):
                        heappush(
                            self._queue,
                            AgentTask(
                                priority=int(item.get("priority", 100)),
                                task_id=str(item.get("task_id")),
                                role=str(item.get("role")),
                                step_name=str(item.get("step_name")),
                                payload=dict(item.get("payload", {})),
                                status=str(item.get("status", "queued")),
                                lease_owner=item.get("lease_owner"),
                                lease_until=float(item["lease_until"]) if item.get("lease_until") is not None else None,
                                attempts=int(item.get("attempts", 0)),
                            ),
                        )
            except Exception:
                self._queue = []
        self.hydrate_from_durable()
        self.flush()

    def _record_task(self, task: AgentTask) -> None:
        if self.durable and self.run_id:
            self.durable.record_task(self.run_id, task)

    def hydrate_from_durable(self) -> None:
        if not self.durable or not self.run_id:
            return
        existing: dict[str, AgentTask] = {task.task_id: task for task in self._queue}
        for item in self.durable.list_tasks(self.run_id):
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            existing[str(item["task_id"])] = AgentTask(
                priority=int(item.get("priority", 100)),
                task_id=str(item["task_id"]),
                role=str(item.get("role", "")),
                step_name=str(item.get("step_name", "")),
                payload=payload,
                status=str(item.get("status", "queued")),
                lease_owner=item.get("lease_owner"),
                lease_until=float(item["lease_until"]) if item.get("lease_until") is not None else None,
                attempts=int(item.get("attempts", 0)),
            )
        self._queue = []
        for task in existing.values():
            heappush(self._queue, task)

    def submit(self, *, task_id: str, role: str, step_name: str, payload: dict[str, Any] | None = None, priority: int = 100) -> AgentTask:
        task = AgentTask(priority=priority, task_id=task_id, role=role, step_name=step_name, payload=payload or {})
        heappush(self._queue, task)
        self.history.append({"event": "task_queued", "task": task.to_dict(), "timestamp": utc_timestamp()})
        self._record_task(task)
        self.flush()
        return task

    def reclaim_expired_leases(self, *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        count = 0
        for task in self._queue:
            if task.status == "running" and task.lease_until is not None and task.lease_until < now:
                task.status = "queued"
                task.lease_owner = None
                task.lease_until = None
                count += 1
                self.history.append({"event": "task_lease_expired", "task": task.to_dict(), "timestamp": utc_timestamp()})
                self._record_task(task)
        if count:
            self.flush()
        return count

    def pop_ready(
        self,
        roles: Iterable[str] | None = None,
        *,
        worker_id: str | None = None,
        lease_s: int = 300,
    ) -> AgentTask | None:
        self.reclaim_expired_leases()
        allowed = set(roles) if roles is not None else None
        skipped: list[AgentTask] = []
        selected: AgentTask | None = None
        while self._queue:
            task = heappop(self._queue)
            if task.status == "queued" and (allowed is None or task.role in allowed):
                task.status = "running"
                task.lease_owner = worker_id
                task.lease_until = time.time() + lease_s
                task.attempts += 1
                self.history.append({"event": "task_leased", "task": task.to_dict(), "worker_id": worker_id, "lease_s": lease_s, "timestamp": utc_timestamp()})
                self._record_task(task)
                selected = task
                break
            skipped.append(task)
        for task in skipped:
            heappush(self._queue, task)
        if selected is not None:
            heappush(self._queue, selected)
        self.flush()
        return selected

    def complete(self, task_id: str, status: str) -> None:
        found = False
        for task in self._queue:
            if task.task_id == task_id:
                task.status = status
                task.lease_owner = None
                task.lease_until = None
                found = True
                self._record_task(task)
        self.history.append({"event": "task_completed", "task_id": task_id, "status": status, "found_in_queue": found, "timestamp": utc_timestamp()})
        self.flush()

    def complete_leased(
        self,
        task_id: str,
        *,
        worker_id: str,
        status: str,
        returncode: int | None = None,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        for task in self._queue:
            if task.task_id != task_id:
                continue
            if task.status != "running" or task.lease_owner != worker_id:
                outcome = {"ok": False, "reason": "lease_not_owned", "task": task.to_dict()}
                self.history.append({"event": "task_complete_rejected", "task_id": task_id, "worker_id": worker_id, "result": outcome, "timestamp": utc_timestamp()})
                self.flush()
                return outcome
            task.status = status
            task.lease_owner = None
            task.lease_until = None
            self._record_task(task)
            outcome = {
                "ok": True,
                "task": task.to_dict(),
                "worker_id": worker_id,
                "status": status,
                "returncode": returncode,
                "result": result or {},
            }
            self.history.append({"event": "task_completed", "task_id": task_id, "worker_id": worker_id, "status": status, "returncode": returncode, "result": result or {}, "timestamp": utc_timestamp()})
            self.flush()
            return outcome
        outcome = {"ok": False, "reason": "task_not_found", "task_id": task_id}
        self.history.append({"event": "task_complete_rejected", "task_id": task_id, "worker_id": worker_id, "result": outcome, "timestamp": utc_timestamp()})
        self.flush()
        return outcome

    def heartbeat(self, task_id: str, *, worker_id: str, lease_s: int = 300) -> dict[str, Any]:
        for task in self._queue:
            if task.task_id == task_id:
                if task.status != "running" or task.lease_owner != worker_id:
                    result = {"ok": False, "reason": "lease_not_owned", "task": task.to_dict()}
                else:
                    task.lease_until = time.time() + lease_s
                    result = {"ok": True, "task": task.to_dict(), "lease_s": lease_s}
                    self._record_task(task)
                self.history.append({"event": "task_heartbeat", "task_id": task_id, "worker_id": worker_id, "result": result, "timestamp": utc_timestamp()})
                self.flush()
                return result
        result = {"ok": False, "reason": "task_not_found", "task_id": task_id}
        self.history.append({"event": "task_heartbeat", "task_id": task_id, "worker_id": worker_id, "result": result, "timestamp": utc_timestamp()})
        self.flush()
        return result

    def queued_command_tasks(self) -> list[AgentTask]:
        return [
            task
            for task in sorted(self._queue)
            if task.status == "queued"
            and isinstance(task.payload, dict)
            and isinstance(task.payload.get("command"), list)
            and all(isinstance(item, str) for item in task.payload.get("command", []))
        ]

    def record_lifecycle(
        self,
        *,
        event: str,
        step_name: str,
        role: str,
        task_id: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.history.append(
            {
                "event": event,
                "task_id": task_id,
                "role": role,
                "step_name": step_name,
                "payload": payload or {},
                "timestamp": utc_timestamp(),
            }
        )
        self.flush()

    def flush(self) -> None:
        data = {"schema_version": SCHEMA_VERSION, "tasks": [task.to_dict() for task in sorted(self._queue)], "history": self.history}
        self.path.write_text(stable_json(data) + "\n", encoding="utf-8")


class HumanGate:
    """Filesystem-backed human-in-the-loop pause/resume gate."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.path = self.root / "human_gate.json"

    def request(self, *, run_id: str, step_name: str, reason: str, artifacts: list[str]) -> dict[str, Any]:
        request = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "step_name": step_name,
            "status": "waiting",
            "reason": reason,
            "artifacts": artifacts,
            "requested_at": utc_timestamp(),
            "resume_token": hashlib.sha256(f"{run_id}:{step_name}:{time.time()}".encode("utf-8")).hexdigest()[:16],
        }
        self.path.write_text(stable_json(request) + "\n", encoding="utf-8")
        return request

    def approve(self, *, approved_by: str, note: str = "") -> dict[str, Any]:
        data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {}
        data.update({"status": "approved", "approved_by": approved_by, "note": note, "approved_at": utc_timestamp()})
        self.path.write_text(stable_json(data) + "\n", encoding="utf-8")
        return data

    def is_approved(self) -> bool:
        if not self.path.exists():
            return False
        data = json.loads(self.path.read_text(encoding="utf-8"))
        return data.get("status") == "approved"


@dataclass
class AgentRun:
    run_id: str
    intent: str
    artifact_root: Path
    environment_fingerprint: dict[str, Any]
    graph: ExecutionGraph = field(default_factory=ExecutionGraph.default)
    status: str = "running"
    start_time: str = field(default_factory=utc_timestamp)
    end_time: str | None = None
    steps: list[AgentStep] = field(default_factory=list)
    store: ArtifactStore = field(init=False)
    durable: DurableStore = field(init=False)
    event_bus: TypedEventBus = field(init=False)
    trace: TraceEmitter = field(init=False)
    memory: MemoryStore = field(init=False)
    scheduler: AgentScheduler = field(init=False)
    human_gate: HumanGate = field(init=False)
    capabilities: CapabilityTable = field(default_factory=CapabilityTable)
    checkpoint_on_init: bool = field(default=True, repr=False)

    def __post_init__(self) -> None:
        self.artifact_root.mkdir(parents=True, exist_ok=True)
        self.store = ArtifactStore(self.artifact_root)
        self.durable = DurableStore(self.artifact_root)
        self.event_bus = TypedEventBus(self.artifact_root, durable=self.durable)
        self.trace = TraceEmitter(self.artifact_root)
        self.memory = MemoryStore(self.artifact_root)
        self.scheduler = AgentScheduler(self.artifact_root, durable=self.durable, run_id=self.run_id)
        self.human_gate = HumanGate(self.artifact_root)
        if not self.checkpoint_on_init:
            return
        self.store.register(self.durable.path, artifact_type="durable_runtime_db", producer="AgentRun")
        self.store.register(self.event_bus.path, artifact_type="typed_event_bus_jsonl", producer="AgentRun")
        self.store.register(self.event_bus.index_path, artifact_type="typed_event_bus_json", producer="AgentRun")
        self.store.register(self.trace.jsonl_path, artifact_type="runtime_trace_jsonl", producer="AgentRun")
        self.store.register(self.trace.json_path, artifact_type="runtime_trace_json", producer="AgentRun")
        self.store.register(self.memory.path, artifact_type="memory_store", producer="AgentRun")
        self.store.register(self.scheduler.path, artifact_type="agent_scheduler", producer="AgentRun")
        self.checkpoint()

    @property
    def checkpoint_path(self) -> Path:
        return self.artifact_root / "agent_run.json"

    def start_step(
        self,
        *,
        name: str,
        role: str,
        required_capabilities: list[str] | None = None,
        input_artifacts: list[str | Path] | None = None,
        output_artifacts: list[str | Path] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentStep:
        required = required_capabilities or []
        source_role = self.previous_completed_role()
        handoff_id = hashlib.sha256(f"{self.run_id}:{len(self.steps)}:{source_role}:{role}:{name}".encode("utf-8")).hexdigest()[:16]
        step = AgentStep(
            name=name,
            role=role,
            required_capabilities=required,
            input_artifacts=[str(path) for path in input_artifacts or []],
            output_artifacts=[str(path) for path in output_artifacts or []],
            metadata=metadata or {},
        )
        step.metadata.setdefault("handoff_id", handoff_id)
        graph_node = self.graph.step_node_for(name, role)
        if graph_node:
            step.metadata.setdefault("execution_graph_node", graph_node.get("id"))
        step.metadata.setdefault("execution_graph_declared", bool(graph_node))
        step.capability_verdict = self.capabilities.require(role, required)
        self.steps.append(step)
        task_id = f"{self.run_id}:{len(self.steps) - 1}:{name}"
        step.metadata.setdefault("scheduler_task_id", task_id)
        input_records = [
            self.store.register(
                artifact,
                artifact_type="step_input",
                producer=None,
                consumer=role,
                step_name=name,
                handoff_id=handoff_id,
                artifact_role="input",
                required=True,
            ).to_dict()
            for artifact in step.input_artifacts
        ]
        self.scheduler.record_lifecycle(
            event="step_scheduled",
            step_name=name,
            role=role,
            task_id=task_id,
            payload={"input_artifacts": step.input_artifacts, "required_capabilities": required},
        )
        self.scheduler.record_lifecycle(
            event="step_started",
            step_name=name,
            role=role,
            task_id=task_id,
            payload={"status": step.status},
        )
        self.trace.span_start(self.run_id, step)
        self.trace.handoff(
            self.run_id,
            source=source_role,
            target=role,
            artifacts=step.input_artifacts,
            reason=f"start_step:{name}",
        )
        self.event_bus.emit(
            run_id=self.run_id,
            event_type="handoff",
            source=source_role,
            target=role,
            artifacts=step.input_artifacts,
            payload={
                "step": step.to_dict(),
                "reason": f"start_step:{name}",
                "handoff_id": handoff_id,
                "artifact_records": input_records,
            },
            correlation_id=handoff_id,
        )
        self.event_bus.emit(
            run_id=self.run_id,
            event_type="step_start",
            source=role,
            target="runtime",
            artifacts=step.input_artifacts,
            payload={"step": step.to_dict(), "artifact_records": input_records},
            correlation_id=str(step.metadata.get("scheduler_task_id")),
        )
        self.durable.record_step(self.run_id, len(self.steps) - 1, step)
        self.checkpoint()
        return step

    def end_step(
        self,
        step: AgentStep,
        *,
        status: str,
        returncode: int | None = None,
        error: str | None = None,
        output_artifacts: list[str | Path] | None = None,
    ) -> None:
        if output_artifacts is not None:
            step.output_artifacts = [str(path) for path in output_artifacts]
        step.complete(status=status, returncode=returncode, error=error)
        handoff_id = str(step.metadata.get("handoff_id") or hashlib.sha256(f"{self.run_id}:{step.role}:{step.name}:output".encode("utf-8")).hexdigest()[:16])
        for artifact in step.output_artifacts:
            self.store.register(
                artifact,
                artifact_type="step_output",
                producer=step.role,
                consumer=None,
                step_name=step.name,
                handoff_id=handoff_id,
                artifact_role="output",
                required=True,
            )
        output_records = [
            record.to_dict()
            for artifact in step.output_artifacts
            for record in self.store.find_by_path(artifact)
            if record.step_name == step.name and record.artifact_role == "output"
        ]
        self.scheduler.record_lifecycle(
            event="step_completed",
            step_name=step.name,
            role=step.role,
            task_id=str(step.metadata.get("scheduler_task_id", f"{self.run_id}:{step.name}")),
            payload={"status": status, "returncode": returncode, "error": error},
        )
        self.trace.span_end(self.run_id, step)
        try:
            step_index = self.steps.index(step)
        except ValueError:
            step_index = len(self.steps) - 1
        self.event_bus.emit(
            run_id=self.run_id,
            event_type="step_end",
            source=step.role,
            target="runtime",
            artifacts=step.output_artifacts,
            payload={"step": step.to_dict(), "artifact_records": output_records},
            correlation_id=str(step.metadata.get("scheduler_task_id")),
        )
        self.durable.record_step(self.run_id, step_index, step)
        self.checkpoint()

    def record_step(
        self,
        *,
        name: str,
        role: str,
        status: str,
        required_capabilities: list[str] | None = None,
        input_artifacts: list[str | Path] | None = None,
        output_artifacts: list[str | Path] | None = None,
        metadata: dict[str, Any] | None = None,
        returncode: int | None = None,
        error: str | None = None,
    ) -> AgentStep:
        step = self.start_step(
            name=name,
            role=role,
            required_capabilities=required_capabilities,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            metadata=metadata,
        )
        self.end_step(step, status=status, returncode=returncode, error=error)
        return step

    def execute_step(
        self,
        *,
        name: str,
        role: str,
        executor: Callable[[AgentStep], dict[str, Any] | None],
        required_capabilities: list[str] | None = None,
        input_artifacts: list[str | Path] | None = None,
        output_artifacts: list[str | Path] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> AgentStep:
        """Run a step through the durable runtime boundary.

        Runners should prefer this over ad-hoc start/end calls when the step has
        executable logic. The callable receives the live AgentStep and may return
        status/returncode/error/output_artifacts fields.
        """

        step = self.start_step(
            name=name,
            role=role,
            required_capabilities=required_capabilities,
            input_artifacts=input_artifacts,
            output_artifacts=output_artifacts,
            metadata=metadata,
        )
        try:
            result = executor(step) or {}
            if not isinstance(result, dict):
                result = {"status": "ok", "result": result}
            status = str(result.get("status", "ok"))
            returncode = result.get("returncode")
            error = result.get("error")
            result_outputs = result.get("output_artifacts")
            self.end_step(
                step,
                status=status,
                returncode=int(returncode) if isinstance(returncode, int) else returncode,
                error=str(error) if error is not None else None,
                output_artifacts=result_outputs if isinstance(result_outputs, list) else None,
            )
        except Exception as exc:
            self.end_step(step, status="failed", returncode=1, error=str(exc))
            raise
        return step

    def observed_execution_graph(self) -> dict[str, Any]:
        """Build the graph that was actually observed for this run.

        The declared graph captures the intended control topology. This artifact
        records the concrete step instances, control order, artifact
        dependencies, typed handoffs, and coverage of declared graph nodes.
        """

        nodes: list[dict[str, Any]] = []
        control_edges: list[dict[str, Any]] = []
        artifact_edges: list[dict[str, Any]] = []
        handoff_edges: list[dict[str, Any]] = []
        producer_by_artifact: dict[str, dict[str, Any]] = {}
        declared_ids = {
            str(node.get("id"))
            for node in self.graph.step_nodes
            if isinstance(node, dict) and node.get("id")
        }
        observed_declared_ids: set[str] = set()

        def norm(path: str | Path) -> str:
            try:
                return str(Path(path).resolve())
            except Exception:
                return str(path)

        for index, step in enumerate(self.steps):
            declared_id = step.metadata.get("execution_graph_node") if isinstance(step.metadata, dict) else None
            if declared_id:
                observed_declared_ids.add(str(declared_id))
            node_id = f"runtime_step.{index}.{step.name}"
            nodes.append(
                {
                    "id": node_id,
                    "index": index,
                    "name": step.name,
                    "role": step.role,
                    "status": step.status,
                    "returncode": step.returncode,
                    "declared_graph_node": declared_id,
                    "declared": bool(step.metadata.get("execution_graph_declared")) if isinstance(step.metadata, dict) else False,
                    "input_artifacts": step.input_artifacts,
                    "output_artifacts": step.output_artifacts,
                    "required_capabilities": step.required_capabilities,
                }
            )
            if index > 0:
                prev = self.steps[index - 1]
                control_edges.append(
                    {
                        "from": f"runtime_step.{index - 1}.{prev.name}",
                        "to": node_id,
                        "type": "observed_control_order",
                    }
                )
            for artifact in step.input_artifacts:
                producer = producer_by_artifact.get(norm(artifact))
                if producer:
                    artifact_edges.append(
                        {
                            "from": producer["node_id"],
                            "to": node_id,
                            "type": "observed_artifact_dependency",
                            "artifact": artifact,
                        }
                    )
            for artifact in step.output_artifacts:
                producer_by_artifact[norm(artifact)] = {"node_id": node_id, "artifact": artifact}

        for event in self.event_bus.events:
            if event.get("run_id") != self.run_id or event.get("event_type") != "handoff":
                continue
            handoff_edges.append(
                {
                    "from": event.get("source"),
                    "to": event.get("target"),
                    "type": "typed_handoff",
                    "correlation_id": event.get("correlation_id"),
                    "artifacts": event.get("artifacts", []),
                }
            )

        undeclared_steps = [
            node
            for node in nodes
            if not node.get("declared")
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status,
            "declared_graph": self.graph.to_dict(),
            "observed_nodes": nodes,
            "observed_control_edges": control_edges,
            "observed_artifact_dependency_edges": artifact_edges,
            "observed_handoff_edges": handoff_edges,
            "summary": {
                "declared_step_node_count": len(declared_ids),
                "observed_step_count": len(nodes),
                "observed_declared_step_count": len(observed_declared_ids),
                "undeclared_step_count": len(undeclared_steps),
                "observed_control_edge_count": len(control_edges),
                "observed_artifact_dependency_edge_count": len(artifact_edges),
                "observed_handoff_edge_count": len(handoff_edges),
                "declared_step_coverage": round(len(observed_declared_ids) / max(len(declared_ids), 1), 3),
            },
        }

    def graph_scheduler_plan(self) -> dict[str, Any]:
        """Describe declared graph readiness from the current runtime state.

        This is a scheduler-readiness artifact, not a replacement executor. It
        makes the native runtime's graph semantics auditable by showing which
        declared steps are already observed, which are runnable, and which are
        still waiting on control or artifact dependencies.
        """

        declared_steps = [
            dict(node)
            for node in self.graph.step_nodes
            if isinstance(node, dict) and node.get("id") and node.get("name") and node.get("role")
        ]
        declared_ids = [str(node["id"]) for node in declared_steps]
        declared_id_set = set(declared_ids)

        predecessors: dict[str, list[str]] = {step_id: [] for step_id in declared_ids}
        successors: dict[str, list[str]] = {step_id: [] for step_id in declared_ids}
        control_edges: list[dict[str, Any]] = []
        for edge in self.graph.step_edges:
            if not isinstance(edge, dict):
                continue
            source = str(edge.get("from", ""))
            target = str(edge.get("to", ""))
            if source not in declared_id_set or target not in declared_id_set:
                continue
            if source not in predecessors[target]:
                predecessors[target].append(source)
            if target not in successors[source]:
                successors[source].append(target)
            control_edges.append({"from": source, "to": target, "type": edge.get("type", "control_dependency")})

        observed_by_declared_id: dict[str, dict[str, Any]] = {}
        observed_by_name_role: dict[tuple[str, str], list[dict[str, Any]]] = {}
        producers_by_artifact: dict[str, list[dict[str, Any]]] = {}

        def norm_artifact(path: str | Path) -> str:
            try:
                return str(self.store.resolve(path).resolve())
            except Exception:
                return str(path)

        for index, step in enumerate(self.steps):
            metadata = step.metadata if isinstance(step.metadata, dict) else {}
            declared_id = str(metadata.get("execution_graph_node") or "")
            observed = {
                "runtime_index": index,
                "runtime_step_id": f"runtime_step.{index}.{step.name}",
                "name": step.name,
                "role": step.role,
                "status": step.status,
                "returncode": step.returncode,
                "error": step.error,
                "input_artifacts": step.input_artifacts,
                "output_artifacts": step.output_artifacts,
                "declared_graph_node": metadata.get("execution_graph_node"),
            }
            if declared_id in declared_id_set:
                observed_by_declared_id[declared_id] = observed
            observed_by_name_role.setdefault((step.name, step.role), []).append(observed)
            for artifact in step.output_artifacts:
                producers_by_artifact.setdefault(norm_artifact(artifact), []).append(
                    {
                        "artifact": str(artifact),
                        "declared_step_id": declared_id if declared_id in declared_id_set else None,
                        "runtime_step_id": observed["runtime_step_id"],
                        "runtime_index": index,
                        "name": step.name,
                        "role": step.role,
                        "status": step.status,
                    }
                )

        completed_statuses = {"ok", "skipped"}
        terminal_blocking_statuses = {"failed", "blocked", "paused"}
        step_records: list[dict[str, Any]] = []
        status_counts: Counter[str] = Counter()
        next_runnable_steps: list[dict[str, Any]] = []
        blocked_steps: list[dict[str, Any]] = []
        waiting_steps: list[dict[str, Any]] = []
        artifact_dependency_edges: list[dict[str, Any]] = []

        def artifact_ready(path: str | Path, *, consumer_step_id: str, consumer_observed: dict[str, Any] | None) -> dict[str, Any]:
            resolved = self.store.resolve(path)
            records = [record.to_dict() for record in self.store.find_by_path(path)]
            consumer_index = consumer_observed.get("runtime_index") if consumer_observed else None
            producer_candidates = producers_by_artifact.get(norm_artifact(path), [])
            producer = None
            if isinstance(consumer_index, int):
                previous_producers = [
                    candidate
                    for candidate in producer_candidates
                    if isinstance(candidate.get("runtime_index"), int)
                    and int(candidate.get("runtime_index")) < consumer_index
                ]
                if previous_producers:
                    producer = max(previous_producers, key=lambda item: int(item.get("runtime_index", -1)))
            elif producer_candidates:
                producer = producer_candidates[-1]
            consumer_runtime_step_id = consumer_observed.get("runtime_step_id") if consumer_observed else None
            if producer and producer.get("runtime_step_id") != consumer_runtime_step_id:
                artifact_dependency_edges.append(
                    {
                        "from": producer.get("declared_step_id") or producer.get("runtime_step_id"),
                        "to": consumer_step_id,
                        "type": "artifact_dependency",
                        "artifact": str(path),
                        "resolved_path": str(resolved),
                        "producer_declared_step_id": producer.get("declared_step_id"),
                        "producer_runtime_step_id": producer.get("runtime_step_id"),
                        "consumer_declared_step_id": consumer_step_id,
                        "consumer_runtime_step_id": consumer_runtime_step_id,
                        "producer_role": producer.get("role"),
                        "consumer_role": consumer_observed.get("role") if consumer_observed else None,
                        "sha256": file_sha256(resolved),
                        "artifact_records": records,
                    }
                )
            return {
                "path": str(path),
                "resolved_path": str(resolved),
                "exists": resolved.exists(),
                "required": True,
                "artifact_records": records,
                "sha256": file_sha256(resolved),
                "producer_known": producer is not None,
                "producer_declared_step_id": producer.get("declared_step_id") if producer else None,
                "producer_runtime_step_id": producer.get("runtime_step_id") if producer else None,
                "consumer_declared_step_id": consumer_step_id,
                "consumer_runtime_step_id": consumer_runtime_step_id,
            }

        for node in declared_steps:
            step_id = str(node["id"])
            observed = observed_by_declared_id.get(step_id)
            if observed is None:
                candidates = observed_by_name_role.get((str(node.get("name")), str(node.get("role"))), [])
                for candidate in candidates:
                    if candidate.get("declared_graph_node") in declared_id_set:
                        continue
                    observed = candidate
                    break

            pred_ids = predecessors.get(step_id, [])
            missing_predecessors = [
                pred_id
                for pred_id in pred_ids
                if observed_by_declared_id.get(pred_id, {}).get("status") not in completed_statuses
            ]
            blocked_predecessors = [
                pred_id
                for pred_id in pred_ids
                if observed_by_declared_id.get(pred_id, {}).get("status") in terminal_blocking_statuses
            ]
            required_artifacts = []
            missing_required_artifacts = []
            if observed:
                required_artifacts = [
                    artifact_ready(artifact, consumer_step_id=step_id, consumer_observed=observed)
                    for artifact in observed.get("input_artifacts", [])
                ]
                missing_required_artifacts = [
                    artifact.get("path")
                    for artifact in required_artifacts
                    if not artifact.get("exists")
                ]

            if observed:
                scheduler_status = str(observed.get("status") or "observed")
            elif blocked_predecessors:
                scheduler_status = "blocked"
            elif missing_predecessors:
                scheduler_status = "waiting"
            elif missing_required_artifacts:
                scheduler_status = "waiting"
            else:
                scheduler_status = "runnable"

            record = {
                "id": step_id,
                "name": str(node.get("name")),
                "role": str(node.get("role")),
                "declared_index": node.get("index"),
                "status": scheduler_status,
                "observed": observed is not None,
                "observed_step": observed,
                "control_predecessors": pred_ids,
                "control_successors": successors.get(step_id, []),
                "missing_predecessors": missing_predecessors,
                "blocked_predecessors": blocked_predecessors,
                "required_artifacts": required_artifacts,
                "missing_required_artifacts": missing_required_artifacts,
            }
            step_records.append(record)
            status_counts[scheduler_status] += 1
            if scheduler_status == "runnable":
                next_runnable_steps.append(
                    {
                        "id": step_id,
                        "name": record["name"],
                        "role": record["role"],
                        "control_predecessors": pred_ids,
                    }
                )
            elif scheduler_status == "blocked":
                blocked_steps.append(record)
            elif scheduler_status == "waiting":
                waiting_steps.append(record)

        undeclared_observed_steps = []
        for index, step in enumerate(self.steps):
            metadata = step.metadata if isinstance(step.metadata, dict) else {}
            declared_id = str(metadata.get("execution_graph_node") or "")
            if declared_id in declared_id_set:
                continue
            undeclared_observed_steps.append(
                {
                    "runtime_index": index,
                    "runtime_step_id": f"runtime_step.{index}.{step.name}",
                    "name": step.name,
                    "role": step.role,
                    "status": step.status,
                    "returncode": step.returncode,
                    "declared_graph_node": metadata.get("execution_graph_node"),
                }
            )

        replay_candidate_statuses = {"failed", "blocked", "paused"}
        failed_step_replay_candidates: list[dict[str, Any]] = []
        step_records_by_id = {str(record["id"]): record for record in step_records}
        artifact_consumers_by_producer: dict[str, list[dict[str, Any]]] = {}
        for edge in artifact_dependency_edges:
            producer_id = edge.get("producer_declared_step_id") or edge.get("from")
            if not producer_id:
                continue
            artifact_consumers_by_producer.setdefault(str(producer_id), []).append(edge)

        for record in step_records:
            if str(record.get("status")) not in replay_candidate_statuses:
                continue
            step_id = str(record["id"])
            downstream_edges = []
            visited_steps = {step_id}
            visited_edges: set[tuple[str, str, str]] = set()
            queue = [step_id]
            while queue:
                current_id = queue.pop(0)
                for edge in artifact_consumers_by_producer.get(current_id, []):
                    target_id = str(edge.get("consumer_declared_step_id") or edge.get("to") or "")
                    edge_key = (str(edge.get("from")), target_id, str(edge.get("artifact")))
                    if edge_key in visited_edges:
                        continue
                    visited_edges.add(edge_key)
                    downstream_edges.append(edge)
                    if target_id and target_id not in visited_steps:
                        visited_steps.add(target_id)
                        queue.append(target_id)
            downstream_step_ids = [
                str(item["id"])
                for item in step_records
                if str(item["id"]) in visited_steps and str(item["id"]) != step_id
            ]
            downstream_steps = [
                {
                    "id": downstream_id,
                    "name": step_records_by_id.get(downstream_id, {}).get("name"),
                    "role": step_records_by_id.get(downstream_id, {}).get("role"),
                    "status": step_records_by_id.get(downstream_id, {}).get("status"),
                    "observed": step_records_by_id.get(downstream_id, {}).get("observed"),
                }
                for downstream_id in downstream_step_ids
            ]
            candidate_step_ids = [step_id, *downstream_step_ids]
            failed_step_replay_candidates.append(
                {
                    "source_step_id": step_id,
                    "source_name": record["name"],
                    "source_role": record["role"],
                    "source_status": record["status"],
                    "selection_policy": "artifact_dependency_subgraph",
                    "candidate_step_ids": candidate_step_ids,
                    "downstream_step_ids": downstream_step_ids,
                    "downstream_steps": downstream_steps,
                    "downstream_artifact_dependency_edges": downstream_edges,
                    "requires_resume_executor": True,
                }
            )

        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status,
            "generated_at": utc_timestamp(),
            "declared_graph": self.graph.to_dict(),
            "declared_steps": step_records,
            "control_edges": control_edges,
            "artifact_dependency_edges": artifact_dependency_edges,
            "failed_step_replay_candidates": failed_step_replay_candidates,
            "next_runnable_steps": next_runnable_steps,
            "waiting_steps": [
                {"id": item["id"], "name": item["name"], "role": item["role"], "missing_predecessors": item["missing_predecessors"]}
                for item in waiting_steps
            ],
            "blocked_steps": [
                {"id": item["id"], "name": item["name"], "role": item["role"], "blocked_predecessors": item["blocked_predecessors"]}
                for item in blocked_steps
            ],
            "undeclared_observed_steps": undeclared_observed_steps,
            "summary": {
                "declared_step_count": len(step_records),
                "observed_declared_step_count": sum(1 for item in step_records if item["observed"]),
                "undeclared_observed_step_count": len(undeclared_observed_steps),
                "completed_step_count": status_counts.get("ok", 0),
                "skipped_step_count": status_counts.get("skipped", 0),
                "failed_step_count": status_counts.get("failed", 0),
                "blocked_step_count": status_counts.get("blocked", 0),
                "paused_step_count": status_counts.get("paused", 0),
                "running_step_count": status_counts.get("running", 0),
                "waiting_step_count": status_counts.get("waiting", 0),
                "runnable_step_count": status_counts.get("runnable", 0),
                "next_runnable_count": len(next_runnable_steps),
                "control_edge_count": len(control_edges),
                "artifact_dependency_edge_count": len(artifact_dependency_edges),
                "failed_step_replay_candidate_count": len(failed_step_replay_candidates),
                "artifact_input_check_count": sum(len(item["required_artifacts"]) for item in step_records),
                "missing_required_artifact_count": sum(len(item["missing_required_artifacts"]) for item in step_records),
            },
        }

    def finish(self, *, status: str) -> None:
        self.status = status
        self.end_time = utc_timestamp()
        self.checkpoint()
        execution_graph_runtime = self.observed_execution_graph()
        graph_scheduler_plan = self.graph_scheduler_plan()
        (self.artifact_root / "execution_graph_runtime.json").write_text(
            stable_json(execution_graph_runtime) + "\n",
            encoding="utf-8",
        )
        (self.artifact_root / "graph_scheduler_plan.json").write_text(
            stable_json(graph_scheduler_plan) + "\n",
            encoding="utf-8",
        )
        self.trace.export_openai_agents_schema(self.artifact_root / "openai_agents_trace.json", run_id=self.run_id)
        self.trace.export_langgraph_schema(self.artifact_root / "langgraph_state.json", run=self)
        self.trace.export_autogen_schema(self.artifact_root / "autogen_trace.json", run=self)
        self.trace.export_crewai_schema(self.artifact_root / "crewai_trace.json", run=self)
        try:
            (self.artifact_root / "framework_adapter_bundle.json").write_text(
                stable_json(build_framework_adapter_bundle(self.checkpoint_path)) + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            (self.artifact_root / "framework_adapter_bundle.json").write_text(
                stable_json(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "status": "adapter_export_failed",
                        "error": str(exc),
                    }
                )
                + "\n",
                encoding="utf-8",
            )
        self.trace.export_schema_alignment_report(self.artifact_root / "trace_schema_alignment.json", run=self)
        self.store.register(self.trace.jsonl_path, artifact_type="runtime_trace_jsonl", producer="TraceEmitter")
        self.store.register(self.trace.json_path, artifact_type="runtime_trace_json", producer="TraceEmitter")
        self.store.register(self.artifact_root / "openai_agents_trace.json", artifact_type="openai_agents_trace", producer="TraceEmitter")
        self.store.register(self.artifact_root / "langgraph_state.json", artifact_type="langgraph_state", producer="TraceEmitter")
        self.store.register(self.artifact_root / "autogen_trace.json", artifact_type="autogen_trace", producer="TraceEmitter")
        self.store.register(self.artifact_root / "crewai_trace.json", artifact_type="crewai_trace", producer="TraceEmitter")
        self.store.register(self.artifact_root / "framework_adapter_bundle.json", artifact_type="framework_adapter_bundle", producer="framework_adapters")
        self.store.register(self.artifact_root / "trace_schema_alignment.json", artifact_type="trace_schema_alignment", producer="TraceEmitter")
        self.store.register(self.artifact_root / "execution_graph_runtime.json", artifact_type="execution_graph_runtime", producer="AgentRun")
        self.store.register(self.artifact_root / "graph_scheduler_plan.json", artifact_type="graph_scheduler_plan", producer="AgentRun")
        self.durable.export_summary(self.run_id, self.artifact_root / "durable_runtime_summary.json")
        self.store.register(self.durable.path, artifact_type="durable_runtime_db", producer="DurableStore")
        self.store.register(self.artifact_root / "durable_runtime_summary.json", artifact_type="durable_runtime_summary", producer="DurableStore")
        self.store.register(self.event_bus.path, artifact_type="typed_event_bus_jsonl", producer="TypedEventBus")
        self.store.register(self.event_bus.index_path, artifact_type="typed_event_bus_json", producer="TypedEventBus")
        self.store.register(self.memory.path, artifact_type="memory_store", producer="AgentRun")
        self.store.register(self.scheduler.path, artifact_type="agent_scheduler", producer="AgentRun")
        self.checkpoint()

    def previous_completed_role(self) -> str:
        for step in reversed(self.steps[:-1]):
            if step.status in {"ok", "blocked", "failed", "skipped"}:
                return step.role
        return "START"

    def next_resumable_step_names(self) -> list[str]:
        return [step.name for step in self.steps if step.status in {"running", "failed", "blocked", "paused"}]

    def pause_for_human(self, *, step_name: str, reason: str, artifacts: list[str | Path]) -> dict[str, Any]:
        request = self.human_gate.request(
            run_id=self.run_id,
            step_name=step_name,
            reason=reason,
            artifacts=[str(path) for path in artifacts],
        )
        self.status = "paused"
        self.trace.emit("human_gate_requested", {"run_id": self.run_id, "request": request})
        self.event_bus.emit(
            run_id=self.run_id,
            event_type="human_gate_requested",
            source=ROLE_GUARD,
            target="human",
            artifacts=[str(path) for path in artifacts],
            payload={"request": request},
        )
        self.store.register(self.human_gate.path, artifact_type="human_gate", producer="RuntimeGuard")
        self.checkpoint()
        return request

    def approve_human_gate(self, *, approved_by: str, note: str = "") -> dict[str, Any]:
        approval = self.human_gate.approve(approved_by=approved_by, note=note)
        self.trace.emit("human_gate_approved", {"run_id": self.run_id, "approval": approval})
        self.event_bus.emit(
            run_id=self.run_id,
            event_type="human_gate_approved",
            source="human",
            target=ROLE_GUARD,
            artifacts=[str(self.human_gate.path)],
            payload={"approval": approval},
        )
        self.store.register(self.human_gate.path, artifact_type="human_gate", producer="RuntimeGuard")
        self.checkpoint()
        return approval

    def submit_task(self, *, task_id: str, role: str, step_name: str, payload: dict[str, Any] | None = None, priority: int = 100) -> AgentTask:
        self.capabilities.require(role, [CAP_WRITE_ARTIFACT] if role != ROLE_CURATOR else [CAP_CURATE_SKILL])
        task = self.scheduler.submit(task_id=task_id, role=role, step_name=step_name, payload=payload, priority=priority)
        self.trace.emit("agent_task_submitted", {"run_id": self.run_id, "task": task.to_dict()})
        self.event_bus.emit(
            run_id=self.run_id,
            event_type="agent_task_submitted",
            source="runtime",
            target=role,
            artifacts=[],
            payload={"task": task.to_dict()},
        )
        return task

    def resume_plan(self) -> dict[str, Any]:
        failed_or_pending = self.next_resumable_step_names()
        completed = [step.name for step in self.steps if step.status == "ok"]
        plan = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "status": self.status,
            "completed_steps": completed,
            "resumable_steps": failed_or_pending,
            "next_step": failed_or_pending[0] if failed_or_pending else None,
            "checkpoint": str(self.checkpoint_path),
        }
        (self.artifact_root / "resume_plan.json").write_text(stable_json(plan) + "\n", encoding="utf-8")
        return plan

    def checkpoint(self) -> None:
        if hasattr(self, "scheduler"):
            self.scheduler.hydrate_from_durable()
            self.scheduler.flush()
        data = self.to_dict()
        self.checkpoint_path.write_text(stable_json(data) + "\n", encoding="utf-8")
        if hasattr(self, "durable"):
            self.durable.record_checkpoint(self.run_id, self.checkpoint_path, self.status, data)
            self.durable.export_summary(self.run_id, self.artifact_root / "durable_runtime_summary.json")
        if hasattr(self, "trace"):
            self.trace.checkpoint(self.run_id, self.checkpoint_path)
        if hasattr(self, "store"):
            self.store.register(self.checkpoint_path, artifact_type="agent_run_checkpoint", producer="AgentRun")
            if hasattr(self, "durable"):
                self.store.register(self.durable.path, artifact_type="durable_runtime_db", producer="DurableStore")
                self.store.register(self.artifact_root / "durable_runtime_summary.json", artifact_type="durable_runtime_summary", producer="DurableStore")
            if hasattr(self, "event_bus"):
                self.store.register(self.event_bus.path, artifact_type="typed_event_bus_jsonl", producer="TypedEventBus")
                self.store.register(self.event_bus.index_path, artifact_type="typed_event_bus_json", producer="TypedEventBus")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "intent": self.intent,
            "artifact_root": str(self.artifact_root),
            "environment_fingerprint": self.environment_fingerprint,
            "graph": self.graph.to_dict(),
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "steps": [step.to_dict() for step in self.steps],
        }

    @classmethod
    def load(cls, path: Path) -> "AgentRun":
        data = json.loads(path.read_text(encoding="utf-8"))
        graph_data = data.get("graph", {})
        stored_artifact_root = Path(data["artifact_root"])
        artifact_root = path.resolve().parent
        run = cls(
            run_id=data["run_id"],
            intent=data["intent"],
            artifact_root=artifact_root,
            environment_fingerprint={
                **data.get("environment_fingerprint", {}),
                "loaded_artifact_root": str(stored_artifact_root),
                "checkpoint_artifact_root": str(artifact_root),
                "artifact_root_rebased": stored_artifact_root.resolve() != artifact_root.resolve(),
            },
            graph=ExecutionGraph(
                nodes=list(graph_data.get("nodes", [])) or ExecutionGraph.default().nodes,
                edges=[(edge["from"], edge["to"]) for edge in graph_data.get("edges", []) if isinstance(edge, dict)],
                step_nodes=[
                    dict(node)
                    for node in graph_data.get("step_nodes", [])
                    if isinstance(node, dict)
                ],
                step_edges=[
                    dict(edge)
                    for edge in graph_data.get("step_edges", [])
                    if isinstance(edge, dict)
                ],
            ),
            status=data.get("status", "running"),
            start_time=data.get("start_time", utc_timestamp()),
            end_time=data.get("end_time"),
            checkpoint_on_init=False,
        )
        run.steps = []
        for item in data.get("steps", []):
            step = AgentStep(
                name=item["name"],
                role=item["role"],
                required_capabilities=list(item.get("required_capabilities", [])),
                input_artifacts=list(item.get("input_artifacts", [])),
                output_artifacts=list(item.get("output_artifacts", [])),
                metadata=dict(item.get("metadata", {})),
                start_time=item.get("start_time", utc_timestamp()),
                end_time=item.get("end_time"),
                status=item.get("status", "unknown"),
                returncode=item.get("returncode"),
                error=item.get("error"),
                capability_verdict=item.get("capability_verdict"),
            )
            run.steps.append(step)
        return run


class RuntimeReplayExecutor:
    """Replay failed or selected command-backed steps from a checkpoint.

    The replay executor intentionally only replays steps whose metadata contains
    a concrete `command` list. Pure bookkeeping steps can be marked skipped or
    regenerated by their owning code path.
    """

    def __init__(self, checkpoint: Path, *, cwd: Path | None = None, dry_run: bool = False) -> None:
        self.run = AgentRun.load(checkpoint)
        self.cwd = cwd
        self.dry_run = dry_run

    def find_start_index(self, from_step: str | None = None) -> int | None:
        if from_step:
            for index, step in enumerate(self.run.steps):
                if step.name == from_step:
                    return index
            return None
        for index, step in enumerate(self.run.steps):
            if step.status in {"failed", "blocked", "running", "paused"}:
                return index
        return None

    def artifact_record_dicts(self, path: str | Path, *, fallback_role: str, fallback_type: str) -> list[dict[str, Any]]:
        records = [record.to_dict() for record in self.run.store.find_by_path(path)]
        if records:
            return records
        resolved = self.run.store.resolve(path)
        return [
            {
                "path": str(resolved),
                "artifact_type": fallback_type,
                "producer": None,
                "consumer": None,
                "step_name": None,
                "handoff_id": None,
                "artifact_role": fallback_role,
                "required": True,
                "schema_version": SCHEMA_VERSION,
                "sha256": file_sha256(resolved),
                "exists": resolved.exists(),
                "timestamp": utc_timestamp(),
            }
        ]

    @staticmethod
    def _norm_artifact_path(path: str | Path) -> str:
        try:
            return str(Path(path).resolve())
        except Exception:
            return str(path)

    def artifact_dependency_indices(self, *, start_index: int | None = None) -> dict[int, list[int]]:
        """Return downstream step dependencies inferred from artifact handoff.

        An edge A -> B exists when B consumes an artifact emitted by A. This is
        the replay dependency graph; it is deliberately separate from source
        order so replay can skip unrelated later steps.
        """

        producer_by_artifact: dict[str, int] = {}
        dependencies: dict[int, list[int]] = {index: [] for index, _ in enumerate(self.run.steps)}
        for index, step in enumerate(self.run.steps):
            for artifact in step.output_artifacts:
                producer_by_artifact[self._norm_artifact_path(artifact)] = index
            for artifact in step.input_artifacts:
                producer = producer_by_artifact.get(self._norm_artifact_path(artifact))
                if producer is None or producer == index:
                    continue
                if start_index is not None and index < start_index:
                    continue
                dependencies.setdefault(producer, [])
                if index not in dependencies[producer]:
                    dependencies[producer].append(index)
        return {source: sorted(targets) for source, targets in dependencies.items() if targets}

    def dependency_replay_indices(self, start_index: int | None, *, only_failed: bool) -> set[int]:
        if start_index is None:
            return set()
        dependencies = self.artifact_dependency_indices(start_index=start_index)
        selected: set[int] = {start_index}
        queue = [start_index]
        while queue:
            current = queue.pop(0)
            for target in dependencies.get(current, []):
                if target in selected:
                    continue
                selected.add(target)
                queue.append(target)
        if only_failed:
            selected = {index for index in selected if self.run.steps[index].status != "ok"}
            selected.add(start_index)
        return selected

    def build_resume_plan(
        self,
        *,
        from_step: str | None = None,
        only_failed: bool = True,
        drain_queue: bool = False,
        dependency_replay: bool = False,
    ) -> dict[str, Any]:
        start_index = self.find_start_index(from_step)
        planned_steps: list[dict[str, Any]] = []
        skipped_unselected_steps: list[dict[str, Any]] = []
        replay_nodes: list[dict[str, Any]] = []
        replay_edges: list[dict[str, Any]] = []
        execution_order: list[str] = []
        dependency_order: list[dict[str, Any]] = []
        artifact_records: list[dict[str, Any]] = []
        artifact_dependencies = self.artifact_dependency_indices(start_index=start_index)
        selected_indices = self.dependency_replay_indices(start_index, only_failed=only_failed) if dependency_replay else None
        replay_candidate_indices = (
            selected_indices
            if selected_indices is not None
            else (
                set(range(start_index, len(self.run.steps)))
                if start_index is not None
                else set()
            )
        )
        replay_generated_paths = {
            self._norm_artifact_path(artifact)
            for index in replay_candidate_indices
            if 0 <= index < len(self.run.steps)
            for artifact in self.run.steps[index].output_artifacts
        }
        if start_index is not None:
            for index, step in enumerate(self.run.steps[start_index:], start=start_index):
                if selected_indices is not None and index not in selected_indices:
                    skipped_unselected_steps.append(
                        {
                            "index": index,
                            "name": step.name,
                            "role": step.role,
                            "status": step.status,
                            "reason": "outside_artifact_dependency_subgraph",
                        }
                    )
                    continue
                if only_failed and step.status == "ok":
                    continue
                command = step.metadata.get("command") if isinstance(step.metadata, dict) else None
                command_replayable = isinstance(command, list) and all(isinstance(item, str) for item in command)
                input_records = [
                    record
                    for artifact in step.input_artifacts
                    for record in self.artifact_record_dicts(artifact, fallback_role="input", fallback_type="step_input")
                ]
                output_records = [
                    record
                    for artifact in step.output_artifacts
                    for record in self.artifact_record_dicts(artifact, fallback_role="output", fallback_type="step_output")
                ]
                input_hash_checks = self.artifact_hash_checks_from_records(input_records, replay_generated_paths=replay_generated_paths)
                input_hash_verdict = self.input_hash_verdict(input_hash_checks)
                side_effect_class = self.side_effect_class(step.required_capabilities, step_name=step.name, command=command if command_replayable else None)
                side_effect_safe = self.side_effect_safe_for_replay(side_effect_class)
                artifact_records.extend(input_records)
                artifact_records.extend(output_records)
                step_node = {
                    "index": index,
                    "name": step.name,
                    "role": step.role,
                    "status": step.status,
                    "command_replayable": command_replayable,
                    "requires_human_approval": bool(step.metadata.get("requires_human_approval")),
                    "input_artifacts": step.input_artifacts,
                    "output_artifacts": step.output_artifacts,
                    "input_artifact_records": input_records,
                    "output_artifact_records": output_records,
                    "input_artifact_hash_checks": input_hash_checks,
                    "input_hash_verdict": input_hash_verdict,
                    "side_effect_class": side_effect_class,
                    "side_effect_safe": side_effect_safe,
                    "replay_mode": self.replay_mode(),
                    "capabilities": step.required_capabilities,
                    "scheduler_task_id": step.metadata.get("scheduler_task_id") if isinstance(step.metadata, dict) else None,
                }
                planned_steps.append(step_node)
                original_id = f"step.{index}.{step.name}"
                replay_id = f"resume.step.{index}.{step.name}"
                execution_order.append(replay_id)
                replay_nodes.append({"id": original_id, "type": "checkpoint_step", **step_node})
                replay_nodes.append({"id": replay_id, "type": "replay_action", **step_node})
                replay_edges.append({"from": original_id, "to": replay_id, "type": "resume_dependency", "reason": "checkpoint_step_selected"})
                if selected_indices is not None:
                    replay_edges.append(
                        {
                            "from": "selection.artifact_dependency_subgraph",
                            "to": replay_id,
                            "type": "replay_selection",
                            "reason": "selected_by_artifact_dependency_subgraph",
                        }
                    )
                for target_index in artifact_dependencies.get(index, []):
                    if selected_indices is not None and target_index not in selected_indices:
                        continue
                    replay_edges.append(
                        {
                            "from": replay_id,
                            "to": f"resume.step.{target_index}.{self.run.steps[target_index].name}",
                            "type": "downstream_artifact_dependency",
                            "reason": "producer_output_consumed_by_downstream_step",
                        }
                    )
                for artifact in step.input_artifacts:
                    artifact_id = f"artifact.input.{index}.{Path(artifact).name}"
                    records = self.artifact_record_dicts(artifact, fallback_role="input", fallback_type="step_input")
                    replay_nodes.append({"id": artifact_id, "type": "input_artifact", "path": artifact, "records": records, "exists": Path(artifact).exists(), "sha256": file_sha256(Path(artifact))})
                    replay_edges.append({"from": artifact_id, "to": replay_id, "type": "artifact_dependency"})
                for artifact in step.output_artifacts:
                    artifact_id = f"artifact.output.{index}.{Path(artifact).name}"
                    records = self.artifact_record_dicts(artifact, fallback_role="output", fallback_type="step_output")
                    replay_nodes.append({"id": artifact_id, "type": "expected_output_artifact", "path": artifact, "records": records, "exists": Path(artifact).exists(), "sha256": file_sha256(Path(artifact))})
                    replay_edges.append({"from": replay_id, "to": artifact_id, "type": "artifact_generation"})
        queued_tasks = [task.to_dict() for task in self.run.scheduler.queued_command_tasks()] if drain_queue else []
        durable_steps = self.run.durable.list_steps(self.run.run_id)
        durable_tasks = self.run.durable.list_tasks(self.run.run_id)
        durable_resumable_steps = [
            step
            for step in durable_steps
            if step.get("status") in {"running", "failed", "blocked", "paused"}
            and (start_index is None or int(step.get("step_index", -1)) >= start_index)
        ]
        durable_resumable_tasks = [
            task
            for task in durable_tasks
            if task.get("status") in {"queued", "running"}
            and isinstance(task.get("payload"), dict)
            and isinstance(task.get("payload", {}).get("command"), list)
        ]
        for item in queued_tasks:
            task_id = str(item.get("task_id"))
            task_node = f"task.{task_id}"
            replay_task_node = f"resume.task.{task_id}"
            execution_order.append(replay_task_node)
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
            command = payload.get("command") if isinstance(payload, dict) else None
            command_replayable = isinstance(command, list) and all(isinstance(part, str) for part in command)
            task_input_records: list[dict[str, Any]] = []
            for artifact in payload.get("input_artifacts", []) if isinstance(payload, dict) else []:
                artifact_id = f"artifact.task_input.{task_id}.{Path(str(artifact)).name}"
                records = self.artifact_record_dicts(str(artifact), fallback_role="input", fallback_type="task_input")
                artifact_records.extend(records)
                task_input_records.extend(records)
                replay_nodes.append({"id": artifact_id, "type": "input_artifact", "path": str(artifact), "records": records, "exists": Path(str(artifact)).exists(), "sha256": file_sha256(Path(str(artifact)))})
                replay_edges.append({"from": artifact_id, "to": replay_task_node, "type": "artifact_dependency"})
            for artifact in payload.get("output_artifacts", []) if isinstance(payload, dict) else []:
                artifact_id = f"artifact.task_output.{task_id}.{Path(str(artifact)).name}"
                records = self.artifact_record_dicts(str(artifact), fallback_role="output", fallback_type="task_output")
                artifact_records.extend(records)
                replay_nodes.append({"id": artifact_id, "type": "expected_output_artifact", "path": str(artifact), "records": records, "exists": Path(str(artifact)).exists(), "sha256": file_sha256(Path(str(artifact)))})
                replay_edges.append({"from": replay_task_node, "to": artifact_id, "type": "artifact_generation"})
            task_hash_checks = self.artifact_hash_checks_from_records(task_input_records, replay_generated_paths=replay_generated_paths)
            item["input_artifact_hash_checks"] = task_hash_checks
            item["input_hash_verdict"] = self.input_hash_verdict(task_hash_checks)
            item["side_effect_class"] = self.side_effect_class(
                payload.get("required_capabilities", []) if isinstance(payload.get("required_capabilities", []), list) else [],
                step_name=str(item.get("step_name")),
                command=command if command_replayable else None,
            )
            item["side_effect_safe"] = self.side_effect_safe_for_replay(str(item["side_effect_class"]))
            item["replay_mode"] = self.replay_mode()
            replay_nodes.append({"id": task_node, "type": "durable_task", **item})
            replay_nodes.append({"id": replay_task_node, "type": "queued_task_replay", **item})
            replay_edges.append({"from": task_node, "to": replay_task_node, "type": "resume_dependency", "reason": "queued_command_task"})
        for i in range(max(len(planned_steps) - 1, 0)):
            replay_edges.append(
                {
                    "from": f"resume.step.{planned_steps[i]['index']}.{planned_steps[i]['name']}",
                    "to": f"resume.step.{planned_steps[i + 1]['index']}.{planned_steps[i + 1]['name']}",
                    "type": "resume_order",
                }
            )
        dependency_order = [
            edge
            for edge in replay_edges
            if edge.get("type") in {"resume_dependency", "artifact_dependency", "downstream_artifact_dependency"}
        ]
        plan = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run.run_id,
            "status": self.run.status,
            "checkpoint": str(self.run.checkpoint_path),
            "from_step": from_step,
            "only_failed": only_failed,
            "drain_queue": drain_queue,
            "dependency_replay": dependency_replay,
            "start_index": start_index,
            "planned_steps": planned_steps,
            "skipped_unselected_steps": skipped_unselected_steps,
            "queued_command_tasks": queued_tasks,
            "execution_order": execution_order,
            "dependency_order": dependency_order,
            "artifact_records": artifact_records,
            "artifact_dependency_indices": {str(source): targets for source, targets in artifact_dependencies.items()},
            "selected_replay_indices": sorted(selected_indices) if selected_indices is not None else None,
            "selection_policy": "artifact_dependency_subgraph" if dependency_replay else "linear_from_start_index",
            "durable_resume_view": {
                "db_path": str(self.run.durable.path),
                "resumable_steps": durable_resumable_steps,
                "resumable_command_tasks": durable_resumable_tasks,
                "source": "durable_runtime.db",
            },
            "resume_graph": {
                "nodes": replay_nodes,
                "edges": replay_edges,
                "dependency_edges": dependency_order,
                "downstream_artifact_dependency_edges": [edge for edge in replay_edges if edge.get("type") == "downstream_artifact_dependency"],
                "replay_selection_edges": [edge for edge in replay_edges if edge.get("type") == "replay_selection"],
                "execution_edges": [edge for edge in replay_edges if edge.get("type") in {"resume_order", "artifact_generation"}],
                "execution_order": execution_order,
                "artifact_records": artifact_records,
                "replayable_step_count": sum(1 for item in planned_steps if item.get("command_replayable")),
                "non_replayable_step_count": sum(1 for item in planned_steps if not item.get("command_replayable")),
                "skipped_unselected_step_count": len(skipped_unselected_steps),
                "queued_task_replay_count": len(queued_tasks),
            },
        }
        (self.run.artifact_root / "resume_plan.json").write_text(stable_json(plan) + "\n", encoding="utf-8")
        self.run.trace.emit("resume_plan_built", {"run_id": self.run.run_id, "plan": plan})
        self.run.store.register(self.run.artifact_root / "resume_plan.json", artifact_type="resume_plan", producer="RuntimeReplayExecutor")
        return plan

    def output_artifact_checks(self, step: AgentStep) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for artifact in step.output_artifacts:
            path = Path(artifact)
            checks.append(
                {
                    "path": artifact,
                    "exists": path.exists(),
                    "sha256": file_sha256(path),
                }
            )
        return checks

    def input_artifact_checks(self, step: AgentStep) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        for artifact in step.input_artifacts:
            path = Path(artifact)
            checks.append(
                {
                    "path": artifact,
                    "exists": path.exists(),
                    "sha256": file_sha256(path),
                }
            )
        return checks

    def replay_mode(self) -> str:
        return "dry_run_replay" if self.dry_run else "real_replay"

    def artifact_hash_checks_from_records(
        self,
        records: list[dict[str, Any]],
        *,
        replay_generated_paths: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        checks: list[dict[str, Any]] = []
        replay_generated_paths = replay_generated_paths or set()
        for record in records:
            if not isinstance(record, dict):
                continue
            raw_path = record.get("path")
            if not raw_path:
                checks.append(
                    {
                        "path": None,
                        "recorded_sha256": record.get("sha256"),
                        "current_sha256": None,
                        "recorded_exists": record.get("exists"),
                        "current_exists": False,
                        "required": bool(record.get("required", True)),
                        "verdict": "missing",
                    }
                )
                continue
            path = self.run.store.resolve(str(raw_path))
            current_exists = path.exists()
            recorded_sha = record.get("sha256")
            current_sha = file_sha256(path)
            norm_path = self._norm_artifact_path(path)
            if bool(record.get("required", True)) and not current_exists and norm_path in replay_generated_paths:
                verdict = "pending_replay_output"
            elif bool(record.get("required", True)) and not current_exists:
                verdict = "missing"
            elif recorded_sha and current_sha and recorded_sha != current_sha:
                verdict = "changed"
            elif recorded_sha is None and current_sha is not None:
                verdict = "unverified"
            elif recorded_sha is not None and current_sha is None:
                verdict = "missing"
            else:
                verdict = "ok"
            checks.append(
                {
                    "path": str(path),
                    "recorded_sha256": recorded_sha,
                    "current_sha256": current_sha,
                    "recorded_exists": record.get("exists"),
                    "current_exists": current_exists,
                    "required": bool(record.get("required", True)),
                    "artifact_role": record.get("artifact_role"),
                    "artifact_type": record.get("artifact_type"),
                    "producer": record.get("producer"),
                    "consumer": record.get("consumer"),
                    "verdict": verdict,
                }
            )
        return checks

    @staticmethod
    def input_hash_verdict(checks: list[dict[str, Any]]) -> str:
        if not checks:
            return "no_inputs"
        verdicts = {str(check.get("verdict")) for check in checks if isinstance(check, dict)}
        for verdict in ("missing", "changed", "unverified"):
            if verdict in verdicts:
                return verdict
        if "pending_replay_output" in verdicts:
            return "pending_replay_output"
        return "ok"

    @staticmethod
    def side_effect_class(
        capabilities: Iterable[str],
        *,
        step_name: str | None = None,
        command: list[str] | None = None,
    ) -> str:
        caps = set(capabilities)
        command_text = " ".join(command or []).lower()
        name = (step_name or "").lower()
        if CAP_LOAD_SCX in caps or "scx_" in command_text or "sched_ext" in command_text:
            return "scx_load"
        if CAP_RESTART_SERVICE in caps or "systemctl" in command_text or "restart" in name:
            return "service_restart"
        if CAP_ROOT_CONTROL in caps or "sudo" in command_text:
            return "root_control"
        if CAP_RUN_BENCHMARK in caps or "sysbench" in command_text or "benchmark" in name:
            return "benchmark"
        return "none"

    def side_effect_safe_for_replay(self, side_effect_class: str) -> bool:
        return self.dry_run or side_effect_class == "none"

    def build_resume_audit(
        self,
        *,
        plan: dict[str, Any],
        replayed: list[dict[str, Any]],
        status: str,
        from_step: str | None,
        only_failed: bool,
        drain_queue: bool,
        dependency_replay: bool = False,
    ) -> dict[str, Any]:
        replay_by_step: dict[str, list[dict[str, Any]]] = {}
        replay_by_task: dict[str, list[dict[str, Any]]] = {}
        for item in replayed:
            if item.get("step"):
                replay_by_step.setdefault(str(item["step"]), []).append(item)
            if item.get("task"):
                replay_by_task.setdefault(str(item["task"]), []).append(item)

        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        checks: list[dict[str, Any]] = []

        def add_node(node_id: str, node_type: str, **fields: Any) -> None:
            nodes.append({"id": node_id, "type": node_type, **fields})

        def add_edge(source: str, target: str, edge_type: str, **fields: Any) -> None:
            edges.append({"from": source, "to": target, "type": edge_type, **fields})

        plan_steps = plan.get("planned_steps", []) if isinstance(plan.get("planned_steps"), list) else []
        plan_resume_graph = plan.get("resume_graph", {}) if isinstance(plan.get("resume_graph"), dict) else {}
        plan_replay_selection_edges = (
            plan_resume_graph.get("replay_selection_edges", [])
            if isinstance(plan_resume_graph.get("replay_selection_edges", []), list)
            else []
        )
        skipped_unselected_steps = (
            plan.get("skipped_unselected_steps", [])
            if isinstance(plan.get("skipped_unselected_steps", []), list)
            else []
        )
        for item in plan_steps:
            if not isinstance(item, dict):
                continue
            step_name = str(item.get("name"))
            step_id = f"step.{item.get('index')}.{step_name}"
            replay_id = f"resume.step.{item.get('index')}.{step_name}"
            outcomes = replay_by_step.get(step_name, [])
            replay_status = outcomes[-1].get("status") if outcomes else "not_replayed"
            command_replayable = bool(item.get("command_replayable"))
            missing_inputs = [
                artifact
                for artifact in item.get("input_artifacts", [])
                if artifact and not Path(str(artifact)).exists()
            ]
            input_hash_checks = item.get("input_artifact_hash_checks", [])
            if not isinstance(input_hash_checks, list):
                input_hash_checks = []
            input_hash_verdict = str(item.get("input_hash_verdict") or self.input_hash_verdict(input_hash_checks))
            side_effect_class = str(item.get("side_effect_class") or "unknown")
            side_effect_safe = bool(item.get("side_effect_safe"))
            check = {
                "scope": "checkpoint_step",
                "step": step_name,
                "index": item.get("index"),
                "checkpoint_status": item.get("status"),
                "command_replayable": command_replayable,
                "requires_human_approval": bool(item.get("requires_human_approval")),
                "input_artifact_count": len(item.get("input_artifacts", [])),
                "missing_input_artifacts": missing_inputs,
                "input_artifact_hash_checks": input_hash_checks,
                "input_hash_verdict": input_hash_verdict,
                "output_artifact_count": len(item.get("output_artifacts", [])),
                "side_effect_class": side_effect_class,
                "side_effect_safe": side_effect_safe,
                "replay_mode": item.get("replay_mode") or self.replay_mode(),
                "replay_status": replay_status,
                "replay_outcomes": outcomes,
                "safe_to_replay": command_replayable and input_hash_verdict in {"ok", "no_inputs", "pending_replay_output"} and side_effect_safe,
            }
            checks.append(check)
            add_node(step_id, "checkpoint_step", **item)
            add_node(replay_id, "resume_action", replay_status=replay_status, safe_to_replay=check["safe_to_replay"])
            add_edge(step_id, replay_id, "resume_dependency", replay_status=replay_status)
            for selection_edge in plan_replay_selection_edges:
                if isinstance(selection_edge, dict) and selection_edge.get("to") == replay_id:
                    add_edge(
                        str(selection_edge.get("from")),
                        replay_id,
                        "replay_selection",
                        reason=selection_edge.get("reason"),
                    )
            for artifact in item.get("input_artifacts", []):
                artifact_id = f"artifact.input.{item.get('index')}.{Path(str(artifact)).name}"
                add_node(
                    artifact_id,
                    "input_artifact",
                    path=str(artifact),
                    exists=Path(str(artifact)).exists(),
                    sha256=file_sha256(Path(str(artifact))),
                )
                add_edge(artifact_id, replay_id, "artifact_dependency")
            for artifact in item.get("output_artifacts", []):
                artifact_id = f"artifact.output.{item.get('index')}.{Path(str(artifact)).name}"
                add_node(
                    artifact_id,
                    "expected_output_artifact",
                    path=str(artifact),
                    exists=Path(str(artifact)).exists(),
                    sha256=file_sha256(Path(str(artifact))),
                )
                add_edge(replay_id, artifact_id, "artifact_generation")

        queued_tasks = plan.get("queued_command_tasks", []) if isinstance(plan.get("queued_command_tasks"), list) else []
        for task in queued_tasks:
            if not isinstance(task, dict):
                continue
            task_id = str(task.get("task_id"))
            task_node = f"task.{task_id}"
            replay_task_node = f"resume.task.{task_id}"
            outcomes = replay_by_task.get(task_id, [])
            replay_status = outcomes[-1].get("status") if outcomes else "not_replayed"
            payload = task.get("payload") if isinstance(task.get("payload"), dict) else {}
            command = payload.get("command") if isinstance(payload, dict) else None
            command_replayable = isinstance(command, list) and all(isinstance(part, str) for part in command)
            input_artifacts = payload.get("input_artifacts", []) if isinstance(payload.get("input_artifacts", []), list) else []
            missing_inputs = [
                artifact
                for artifact in input_artifacts
                if artifact and not Path(str(artifact)).exists()
            ]
            input_hash_checks = task.get("input_artifact_hash_checks", [])
            if not isinstance(input_hash_checks, list):
                input_hash_checks = []
            input_hash_verdict = str(task.get("input_hash_verdict") or self.input_hash_verdict(input_hash_checks))
            side_effect_class = str(task.get("side_effect_class") or "unknown")
            side_effect_safe = bool(task.get("side_effect_safe"))
            checks.append(
                {
                    "scope": "queued_task",
                    "task": task_id,
                    "step": task.get("step_name"),
                    "checkpoint_status": task.get("status"),
                    "command_replayable": command_replayable,
                    "input_artifact_count": len(input_artifacts),
                    "missing_input_artifacts": missing_inputs,
                    "input_artifact_hash_checks": input_hash_checks,
                    "input_hash_verdict": input_hash_verdict,
                    "side_effect_class": side_effect_class,
                    "side_effect_safe": side_effect_safe,
                    "replay_mode": task.get("replay_mode") or self.replay_mode(),
                    "replay_status": replay_status,
                    "replay_outcomes": outcomes,
                    "safe_to_replay": command_replayable and input_hash_verdict in {"ok", "no_inputs", "pending_replay_output"} and side_effect_safe,
                }
            )
            add_node(task_node, "queued_command_task", **task)
            add_node(replay_task_node, "queued_task_replay", replay_status=replay_status)
            add_edge(task_node, replay_task_node, "resume_dependency", replay_status=replay_status)

        unsafe = [item for item in checks if not item.get("safe_to_replay")]
        skipped = [item for item in replayed if item.get("status") == "skipped"]
        failed = [item for item in replayed if item.get("status") == "failed"]
        input_hash_verdicts = [str(item.get("input_hash_verdict", "unknown")) for item in checks]
        side_effect_classes = [str(item.get("side_effect_class", "unknown")) for item in checks]
        audit = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run.run_id,
            "checkpoint": str(self.run.checkpoint_path),
            "status": status,
            "from_step": from_step,
            "only_failed": only_failed,
            "drain_queue": drain_queue,
            "dependency_replay": dependency_replay,
            "dry_run": self.dry_run,
            "resume_plan": str(self.run.artifact_root / "resume_plan.json"),
            "selection_policy": plan.get("selection_policy"),
            "selected_replay_indices": plan.get("selected_replay_indices"),
            "artifact_dependency_indices": plan.get("artifact_dependency_indices"),
            "execution_order": plan.get("execution_order", []),
            "dependency_order": plan.get("dependency_order", []),
            "artifact_records": plan.get("artifact_records", []),
            "summary": {
                "planned_step_count": len(plan_steps),
                "queued_task_count": len(queued_tasks),
                "replayed_count": len(replayed),
                "safe_to_replay_count": sum(1 for item in checks if item.get("safe_to_replay")),
                "unsafe_to_replay_count": len(unsafe),
                "skipped_count": len(skipped),
                "failed_count": len(failed),
                "dependency_node_count": len(nodes),
                "dependency_edge_count": len(edges),
                "downstream_artifact_dependency_count": len(plan.get("resume_graph", {}).get("downstream_artifact_dependency_edges", [])) if isinstance(plan.get("resume_graph"), dict) else 0,
                "replay_selection_edge_count": len(plan_replay_selection_edges),
                "skipped_unselected_step_count": len(skipped_unselected_steps),
                "input_hash_changed_count": sum(1 for verdict in input_hash_verdicts if verdict == "changed"),
                "input_hash_missing_count": sum(1 for verdict in input_hash_verdicts if verdict == "missing"),
                "input_hash_unverified_count": sum(1 for verdict in input_hash_verdicts if verdict == "unverified"),
                "input_hash_ok_count": sum(1 for verdict in input_hash_verdicts if verdict in {"ok", "no_inputs"}),
                "input_hash_pending_replay_output_count": sum(1 for verdict in input_hash_verdicts if verdict == "pending_replay_output"),
                "side_effect_safe_count": sum(1 for item in checks if item.get("side_effect_safe")),
                "side_effect_unsafe_count": sum(1 for item in checks if not item.get("side_effect_safe")),
                "scx_load_replay_count": sum(1 for value in side_effect_classes if value == "scx_load"),
                "service_restart_replay_count": sum(1 for value in side_effect_classes if value == "service_restart"),
                "benchmark_replay_count": sum(1 for value in side_effect_classes if value == "benchmark"),
                "root_control_replay_count": sum(1 for value in side_effect_classes if value == "root_control"),
            },
            "skipped_unselected_steps": skipped_unselected_steps,
            "checks": checks,
            "dependency_graph": {
                "nodes": nodes,
                "edges": edges,
                "resume_dependency_edges": [edge for edge in edges if edge.get("type") == "resume_dependency"],
                "artifact_dependency_edges": [edge for edge in edges if edge.get("type") == "artifact_dependency"],
                "artifact_generation_edges": [edge for edge in edges if edge.get("type") == "artifact_generation"],
                "replay_selection_edges": [edge for edge in edges if edge.get("type") == "replay_selection"],
                "downstream_artifact_dependency_edges": plan.get("resume_graph", {}).get("downstream_artifact_dependency_edges", []) if isinstance(plan.get("resume_graph"), dict) else [],
            },
            "replayed": replayed,
        }
        audit_path = self.run.artifact_root / "resume_audit.json"
        audit_path.write_text(stable_json(audit) + "\n", encoding="utf-8")
        self.run.store.register(audit_path, artifact_type="resume_audit", producer="RuntimeReplayExecutor")
        self.run.trace.emit("resume_audit_written", {"run_id": self.run.run_id, "audit": audit})
        self.run.checkpoint()
        return audit

    def run_command_step(self, step: AgentStep, command: list[str]) -> dict[str, Any]:
        self.run.capabilities.require(step.role, step.required_capabilities)
        self.run.trace.emit("step_replay_start", {"run_id": self.run.run_id, "step": step.to_dict(), "dry_run": self.dry_run})
        if self.dry_run:
            returncode = 0
        else:
            proc = subprocess.run(command, cwd=self.cwd, check=False)
            returncode = proc.returncode
        step.complete(
            status="ok" if returncode == 0 else "failed",
            returncode=returncode,
            error=None if returncode == 0 else f"replay failed with return code {returncode}",
        )
        artifact_checks = [] if self.dry_run else self.output_artifact_checks(step)
        for artifact in step.output_artifacts:
            self.run.store.register(artifact, artifact_type="step_output", producer=step.name)
        self.run.trace.emit(
            "step_replay_end",
            {
                "run_id": self.run.run_id,
                "step": step.to_dict(),
                "dry_run": self.dry_run,
                "output_artifact_checks": artifact_checks,
            },
        )
        self.run.checkpoint()
        return {"step": step.name, "status": step.status, "returncode": returncode, "output_artifact_checks": artifact_checks}

    def drain_queued_command_tasks(self) -> list[dict[str, Any]]:
        replayed: list[dict[str, Any]] = []
        while True:
            task = self.run.scheduler.pop_ready()
            if task is None:
                break
            command = task.payload.get("command") if isinstance(task.payload, dict) else None
            if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                self.run.scheduler.complete(task.task_id, "skipped")
                replayed.append({"task": task.task_id, "step": task.step_name, "status": "skipped", "reason": "missing_command_payload"})
                continue
            step = self.run.start_step(
                name=task.step_name,
                role=task.role,
                required_capabilities=list(task.payload.get("required_capabilities", [])),
                input_artifacts=list(task.payload.get("input_artifacts", [])),
                output_artifacts=list(task.payload.get("output_artifacts", [])),
                metadata={"command": command, "resumed_from_task": task.task_id},
            )
            item = self.run_command_step(step, command)
            item["task"] = task.task_id
            replayed.append(item)
            self.run.scheduler.complete(task.task_id, step.status)
            if step.status != "ok":
                break
        return replayed

    def replay(
        self,
        *,
        from_step: str | None = None,
        only_failed: bool = True,
        drain_queue: bool = False,
        dependency_replay: bool = False,
    ) -> dict[str, Any]:
        plan = self.build_resume_plan(
            from_step=from_step,
            only_failed=only_failed,
            drain_queue=drain_queue,
            dependency_replay=dependency_replay,
        )
        start_index = self.find_start_index(from_step)
        selected_indices = set(plan.get("selected_replay_indices") or []) if dependency_replay else None
        if start_index is None and not plan["queued_command_tasks"]:
            result = {
                "schema_version": SCHEMA_VERSION,
                "run_id": self.run.run_id,
                "status": "nothing_to_resume",
                "from_step": from_step,
                "replayed": [],
            }
            self.build_resume_audit(
                plan=plan,
                replayed=[],
                status=str(result["status"]),
                from_step=from_step,
                only_failed=only_failed,
                drain_queue=drain_queue,
                dependency_replay=dependency_replay,
            )
            (self.run.artifact_root / "resume_result.json").write_text(stable_json(result) + "\n", encoding="utf-8")
            return result

        replayed: list[dict[str, Any]] = []
        if start_index is not None:
            for index, step in enumerate(self.run.steps[start_index:], start=start_index):
                if selected_indices is not None and index not in selected_indices:
                    continue
                if only_failed and step.status == "ok":
                    continue
                if step.status == "paused" and step.metadata.get("requires_human_approval"):
                    if not self.run.human_gate.is_approved():
                        result = {
                            "schema_version": SCHEMA_VERSION,
                            "run_id": self.run.run_id,
                            "status": "waiting_for_human_approval",
                            "from_step": from_step,
                            "only_failed": only_failed,
                            "dry_run": self.dry_run,
                            "blocked_step": step.name,
                            "human_gate": str(self.run.human_gate.path),
                            "replayed": replayed,
                            "resume_plan": str(self.run.artifact_root / "resume_plan.json"),
                        }
                        self.build_resume_audit(
                            plan=plan,
                            replayed=replayed,
                            status=str(result["status"]),
                            from_step=from_step,
                            only_failed=only_failed,
                            drain_queue=drain_queue,
                            dependency_replay=dependency_replay,
                        )
                        (self.run.artifact_root / "resume_result.json").write_text(stable_json(result) + "\n", encoding="utf-8")
                        return result
                    step.complete(status="ok", returncode=0)
                    replayed.append({"step": step.name, "status": "ok", "returncode": 0, "approved_human_gate": True})
                    self.run.checkpoint()
                    continue
                command = step.metadata.get("command") if isinstance(step.metadata, dict) else None
                if not isinstance(command, list) or not all(isinstance(item, str) for item in command):
                    if step.status == "blocked" and self.run.human_gate.is_approved():
                        step.complete(status="skipped", returncode=0, error="superseded_by_human_approval")
                        replayed.append({"step": step.name, "status": "skipped", "reason": "superseded_by_human_approval"})
                        self.run.checkpoint()
                        continue
                    replayed.append({"step": step.name, "status": "skipped", "reason": "missing_command_metadata"})
                    continue

                item = self.run_command_step(step, command)
                replayed.append(item)
                if item["returncode"] != 0:
                    break

        queued_replayed = self.drain_queued_command_tasks() if drain_queue else []
        replayed.extend(queued_replayed)
        self.run.finish(status="ok" if all(item.get("status") in {"ok", "skipped"} for item in replayed) else "failed")
        result = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run.run_id,
            "status": self.run.status,
            "from_step": from_step,
            "only_failed": only_failed,
            "drain_queue": drain_queue,
            "dependency_replay": dependency_replay,
            "selection_policy": plan.get("selection_policy"),
            "selected_replay_indices": plan.get("selected_replay_indices"),
            "dry_run": self.dry_run,
            "replayed": replayed,
            "resume_plan": str(self.run.artifact_root / "resume_plan.json"),
        }
        result["resume_audit"] = str(self.run.artifact_root / "resume_audit.json")
        self.build_resume_audit(
            plan=plan,
            replayed=replayed,
            status=str(result["status"]),
            from_step=from_step,
            only_failed=only_failed,
            drain_queue=drain_queue,
            dependency_replay=dependency_replay,
        )
        (self.run.artifact_root / "resume_result.json").write_text(stable_json(result) + "\n", encoding="utf-8")
        return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("--checkpoint", type=Path, required=True)
    resume.add_argument("--from-step")
    resume.add_argument("--cwd", type=Path)
    resume.add_argument("--dry-run", action="store_true")
    resume.add_argument("--all-from-step", action="store_true", help="Replay all steps after --from-step, including successful ones")
    resume.add_argument("--continue-downstream", action="store_true", help="Replay all command-backed steps downstream of the first resumable step")
    resume.add_argument("--drain-queue", action="store_true", help="Execute queued command tasks after replaying checkpoint steps")
    autoresume = sub.add_parser("auto-resume")
    autoresume.add_argument("--checkpoint", type=Path, required=True)
    autoresume.add_argument("--cwd", type=Path)
    autoresume.add_argument("--dry-run", action="store_true")
    submit = sub.add_parser("submit-task")
    submit.add_argument("--checkpoint", type=Path, required=True)
    submit.add_argument("--task-id", required=True)
    submit.add_argument("--role", required=True)
    submit.add_argument("--step-name", required=True)
    submit.add_argument("--payload", default="{}")
    submit.add_argument("--priority", type=int, default=100)
    pop = sub.add_parser("pop-task")
    pop.add_argument("--checkpoint", type=Path, required=True)
    pop.add_argument("--role", action="append")
    pop.add_argument("--worker-id", default="cli-worker")
    pop.add_argument("--lease-s", type=int, default=300)
    heartbeat = sub.add_parser("heartbeat-task")
    heartbeat.add_argument("--checkpoint", type=Path, required=True)
    heartbeat.add_argument("--task-id", required=True)
    heartbeat.add_argument("--worker-id", required=True)
    heartbeat.add_argument("--lease-s", type=int, default=300)
    approve = sub.add_parser("approve-human-gate")
    approve.add_argument("--checkpoint", type=Path, required=True)
    approve.add_argument("--approved-by", required=True)
    approve.add_argument("--note", default="")
    args = ap.parse_args(argv)

    if args.cmd == "resume":
        executor = RuntimeReplayExecutor(args.checkpoint, cwd=args.cwd, dry_run=args.dry_run)
        result = executor.replay(
            from_step=args.from_step,
            only_failed=not (args.all_from_step or args.continue_downstream),
            drain_queue=args.drain_queue,
            dependency_replay=args.continue_downstream,
        )
        print(stable_json(result))
        return 0 if result["status"] in {"ok", "nothing_to_resume"} else 1
    if args.cmd == "auto-resume":
        executor = RuntimeReplayExecutor(args.checkpoint, cwd=args.cwd, dry_run=args.dry_run)
        result = executor.replay(from_step=None, only_failed=False, drain_queue=True)
        print(stable_json(result))
        return 0 if result["status"] in {"ok", "nothing_to_resume"} else 1
    if args.cmd == "submit-task":
        run = AgentRun.load(args.checkpoint)
        payload = json.loads(args.payload)
        task = run.submit_task(task_id=args.task_id, role=args.role, step_name=args.step_name, payload=payload, priority=args.priority)
        run.checkpoint()
        print(stable_json(task.to_dict()))
        return 0
    if args.cmd == "pop-task":
        run = AgentRun.load(args.checkpoint)
        task = run.scheduler.pop_ready(args.role, worker_id=args.worker_id, lease_s=args.lease_s)
        run.checkpoint()
        print(stable_json(task.to_dict() if task else {"task": None}))
        return 0
    if args.cmd == "heartbeat-task":
        run = AgentRun.load(args.checkpoint)
        result = run.scheduler.heartbeat(args.task_id, worker_id=args.worker_id, lease_s=args.lease_s)
        run.checkpoint()
        print(stable_json(result))
        return 0 if result.get("ok") else 1
    if args.cmd == "approve-human-gate":
        run = AgentRun.load(args.checkpoint)
        approval = run.approve_human_gate(approved_by=args.approved_by, note=args.note)
        print(stable_json(approval))
        return 0
    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
