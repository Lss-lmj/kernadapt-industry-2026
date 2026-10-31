#!/usr/bin/env python3
"""HTTP runtime service for LockAgent memory, HITL, resume, and workers."""

from __future__ import annotations

import argparse
import html
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from agent_runtime import DurableStore, RuntimeReplayExecutor, stable_json
from agent_services import (
    hitl_approve,
    hitl_list,
    memory_query,
    memory_retrieve,
    task_list,
    task_submit,
    worker_complete,
    worker_heartbeat,
    worker_lease,
    worker_once,
    write_json,
)
from global_memory import append_record, read_jsonl, summarize
from runtime_api_adapter import invoke_from_body


SCHEMA_VERSION = "lockagent.runtime_service.v0.1"


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def append_service_event(target_dir: Path, event: dict[str, Any]) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "runtime_service_events.jsonl"
    record = {"schema_version": SCHEMA_VERSION, "timestamp": timestamp(), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def registry_path(root: Path) -> Path:
    return root / "worker_registry.json"


def root_durable(root: Path) -> DurableStore:
    return DurableStore(root)


def load_worker_registry(root: Path) -> dict[str, Any]:
    path = registry_path(root)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "workers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    workers = data.get("workers") if isinstance(data, dict) else None
    return {
        "schema_version": SCHEMA_VERSION,
        "workers": workers if isinstance(workers, dict) else {},
    }


def save_worker_registry(root: Path, registry: dict[str, Any]) -> None:
    registry["schema_version"] = SCHEMA_VERSION
    registry["updated_at"] = timestamp()
    write_json(registry_path(root), registry)


def _normalize_roles(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value]
    return []


def register_worker(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    worker_id = str(body["worker_id"])
    now = time.time()
    registry = load_worker_registry(root)
    workers = registry.setdefault("workers", {})
    previous = workers.get(worker_id, {}) if isinstance(workers.get(worker_id), dict) else {}
    workers[worker_id] = {
        "worker_id": worker_id,
        "roles": _normalize_roles(body.get("roles")),
        "status": str(body.get("status", "ready")),
        "metadata": body.get("metadata") if isinstance(body.get("metadata"), dict) else {},
        "registered_at": previous.get("registered_at", timestamp()),
        "registered_epoch": previous.get("registered_epoch", now),
        "last_seen": timestamp(),
        "last_seen_epoch": now,
    }
    save_worker_registry(root, registry)
    root_durable(root).record_worker(workers[worker_id])
    return {"schema_version": SCHEMA_VERSION, "status": "registered", "worker": workers[worker_id]}


def heartbeat_registered_worker(root: Path, body: dict[str, Any]) -> dict[str, Any]:
    worker_id = str(body["worker_id"])
    now = time.time()
    registry = load_worker_registry(root)
    workers = registry.setdefault("workers", {})
    worker = workers.get(worker_id)
    if not isinstance(worker, dict):
        worker = {
            "worker_id": worker_id,
            "roles": _normalize_roles(body.get("roles")),
            "registered_at": timestamp(),
            "registered_epoch": now,
            "metadata": {},
        }
    if body.get("roles") is not None:
        worker["roles"] = _normalize_roles(body.get("roles"))
    if isinstance(body.get("metadata"), dict):
        worker["metadata"] = {**worker.get("metadata", {}), **body["metadata"]}
    worker["status"] = str(body.get("status", worker.get("status", "ready")))
    worker["last_seen"] = timestamp()
    worker["last_seen_epoch"] = now
    workers[worker_id] = worker
    save_worker_registry(root, registry)
    root_durable(root).record_worker(worker)
    return {"schema_version": SCHEMA_VERSION, "status": "heartbeat", "worker": worker}


def list_workers(root: Path, *, active_window_s: int = 600) -> dict[str, Any]:
    now = time.time()
    registry = load_worker_registry(root)
    durable = root_durable(root)
    workers = list(registry.get("workers", {}).values())
    for worker in workers:
        if isinstance(worker, dict):
            last_seen = float(worker.get("last_seen_epoch", 0) or 0)
            worker["seconds_since_last_seen"] = max(0.0, now - last_seen) if last_seen else None
            worker["active"] = bool(last_seen and now - last_seen <= active_window_s)
    return {
        "schema_version": SCHEMA_VERSION,
        "registry": str(registry_path(root)),
        "durable_db": str(durable.path),
        "worker_count": len(workers),
        "active_worker_count": sum(1 for worker in workers if isinstance(worker, dict) and worker.get("active")),
        "workers": workers,
        "durable_workers": durable.list_workers(),
    }


def read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("request body must be a JSON object")
    return data


def as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def service_status(*, root: Path, memory: Path, auth_enabled: bool = False) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "status": "ok",
        "root": str(root),
        "memory": str(memory),
        "auth_enabled": auth_enabled,
        "time": timestamp(),
        "endpoints": [
            "GET /health",
            "GET /memory?failure=...&scheduler=...",
            "GET /memory/summary",
            "GET /memory/retrieve?workload=...&scheduler=...",
            "GET /hitl",
            "GET /ui/hitl",
            "GET /tasks?checkpoint=...",
            "GET /ui/tasks?checkpoint=...",
            "GET /workers",
            "POST /memory/append",
            "POST /hitl/approve",
            "POST /resume",
            "POST /tasks/submit",
            "POST /worker/once",
            "POST /worker/lease",
            "POST /worker/complete",
            "POST /worker/heartbeat",
            "POST /workers/register",
            "POST /workers/heartbeat",
            "POST /adapter/invoke",
        ],
    }


def discover_checkpoints(root: Path) -> list[Path]:
    return sorted(path for path in root.glob("**/agent_run.json") if path.is_file())


def render_tasks_html(root: Path, *, checkpoint: Path | None = None, form_auth_token: str | None = None) -> str:
    token_qs = f"&token={html.escape(form_auth_token)}" if form_auth_token else ""
    if checkpoint is None:
        rows = []
        for item in discover_checkpoints(root):
            cp = html.escape(str(item))
            rows.append(f"<tr><td>{cp}</td><td><a href='/ui/tasks?checkpoint={cp}{token_qs}'>Open</a></td></tr>")
        body = "\n".join(rows) or "<tr><td colspan='2'>No checkpoints</td></tr>"
        return (
            "<!doctype html><html><head><meta charset='utf-8'><title>LockAgent Tasks</title>"
            "<style>body{font-family:sans-serif;margin:2rem}td,th{border:1px solid #ccc;padding:.4rem}table{border-collapse:collapse}</style>"
            "</head><body><h1>LockAgent Task Queues</h1>"
            "<table><thead><tr><th>Checkpoint</th><th>Action</th></tr></thead>"
            f"<tbody>{body}</tbody></table></body></html>"
        )

    data = task_list(checkpoint)
    rows = []
    for item in data.get("tasks", []):
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('task_id', '')))}</td>"
            f"<td>{html.escape(str(item.get('role', '')))}</td>"
            f"<td>{html.escape(str(item.get('step_name', '')))}</td>"
            f"<td>{html.escape(str(item.get('status', '')))}</td>"
            f"<td>{html.escape(str(item.get('lease_owner', '')))}</td>"
            f"<td>{html.escape(str(item.get('attempts', '')))}</td>"
            "</tr>"
        )
    body = "\n".join(rows) or "<tr><td colspan='6'>No tasks</td></tr>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>LockAgent Tasks</title>"
        "<style>body{font-family:sans-serif;margin:2rem}td,th{border:1px solid #ccc;padding:.4rem}table{border-collapse:collapse}</style>"
        "</head><body><h1>LockAgent Task Queue</h1>"
        f"<p>Checkpoint: {html.escape(str(checkpoint))}</p>"
        "<table><thead><tr><th>Task</th><th>Role</th><th>Step</th><th>Status</th><th>Lease Owner</th><th>Attempts</th></tr></thead>"
        f"<tbody>{body}</tbody></table></body></html>"
    )


def render_hitl_html(root: Path, *, form_auth_token: str | None = None) -> str:
    data = hitl_list(root)
    rows = []
    auth_input = (
        f"<input type='hidden' name='auth_token' value='{html.escape(form_auth_token)}' />"
        if form_auth_token
        else ""
    )
    for item in data.get("all", []):
        checkpoint = html.escape(str(item.get("checkpoint", "")))
        status = html.escape(str(item.get("status", "")))
        step = html.escape(str(item.get("step_name", "")))
        reason = html.escape(str(item.get("reason", "")))
        button = ""
        if item.get("status") == "waiting":
            button = (
                "<form method='post' action='/hitl/approve'>"
                f"<input type='hidden' name='checkpoint' value='{checkpoint}' />"
                "<input type='hidden' name='approved_by' value='runtime-service-ui' />"
                "<input type='hidden' name='auto_resume' value='true' />"
                "<input type='hidden' name='dry_run' value='true' />"
                f"{auth_input}"
                "<button type='submit'>Approve + dry-run resume</button>"
                "</form>"
            )
        rows.append(f"<tr><td>{status}</td><td>{step}</td><td>{reason}</td><td>{checkpoint}</td><td>{button}</td></tr>")
    body = "\n".join(rows) or "<tr><td colspan='5'>No HITL requests</td></tr>"
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>LockAgent HITL</title>"
        "<style>body{font-family:sans-serif;margin:2rem}td,th{border:1px solid #ccc;padding:.4rem}table{border-collapse:collapse}</style>"
        "</head><body><h1>LockAgent HITL Queue</h1>"
        "<table><thead><tr><th>Status</th><th>Step</th><th>Reason</th><th>Checkpoint</th><th>Action</th></tr></thead>"
        f"<tbody>{body}</tbody></table></body></html>"
    )


class LockAgentRequestHandler(BaseHTTPRequestHandler):
    server: "LockAgentHTTPServer"

    def log_message(self, fmt: str, *args: Any) -> None:
        append_service_event(self.server.root, {"event": "http_log", "message": fmt % args})

    def send_json(self, data: dict[str, Any], status: int = 200) -> None:
        payload = stable_json(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, text: str, status: int = 200) -> None:
        payload = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"schema_version": SCHEMA_VERSION, "status": "error", "error": message}, status=status)

    def send_auth_error(self) -> None:
        append_service_event(self.server.root, {"event": "http_auth_failure", "path": self.path})
        payload = stable_json({"schema_version": SCHEMA_VERSION, "status": "error", "error": "unauthorized"}).encode("utf-8")
        self.send_response(401)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def authorized(self, *, params: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> bool:
        token = self.server.auth_token
        if not token:
            return True
        header = self.headers.get("Authorization", "")
        if header == f"Bearer {token}":
            return True
        if params and params.get("token") == token:
            return True
        if body and body.get("auth_token") == token:
            return True
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
        try:
            if parsed.path == "/health":
                self.send_json(service_status(root=self.server.root, memory=self.server.memory, auth_enabled=bool(self.server.auth_token)))
                return
            if not self.authorized(params=params):
                self.send_auth_error()
                return
            if parsed.path == "/memory":
                result = memory_query(
                    self.server.memory,
                    scheduler=params.get("scheduler"),
                    failure=params.get("failure"),
                    namespace=params.get("namespace"),
                )
                append_service_event(
                    self.server.root,
                    {"event": "http_memory_query", "memory": str(self.server.memory), "count": result.get("count")},
                )
                self.send_json(result)
                return
            if parsed.path == "/memory/summary":
                records = read_jsonl(self.server.memory)
                result = {
                    "schema_version": SCHEMA_VERSION,
                    "memory": str(self.server.memory),
                    "summary": summarize(records),
                }
                append_service_event(
                    self.server.root,
                    {"event": "http_memory_summary", "memory": str(self.server.memory), "record_count": len(records)},
                )
                self.send_json(result)
                return
            if parsed.path == "/memory/retrieve":
                result = memory_retrieve(
                    self.server.memory,
                    workload=params.get("workload"),
                    scheduler=params.get("scheduler"),
                    skill=params.get("skill"),
                    limit=int(params.get("limit", 8)),
                )
                append_service_event(
                    self.server.root,
                    {
                        "event": "http_memory_retrieve",
                        "memory": str(self.server.memory),
                        "returned_count": result.get("returned_count"),
                        "workload": params.get("workload"),
                        "scheduler": params.get("scheduler"),
                        "skill": params.get("skill"),
                    },
                )
                self.send_json(result)
                return
            if parsed.path == "/hitl":
                self.send_json(hitl_list(self.server.root))
                return
            if parsed.path == "/ui/hitl":
                form_token = params.get("token") if self.server.auth_token and params.get("token") == self.server.auth_token else None
                self.send_html(render_hitl_html(self.server.root, form_auth_token=form_token))
                return
            if parsed.path == "/tasks":
                if not params.get("checkpoint"):
                    self.send_error_json("missing query parameter: checkpoint", status=400)
                    return
                checkpoint = Path(str(params["checkpoint"]))
                result = task_list(checkpoint, status=params.get("status"), role=params.get("role"))
                append_service_event(
                    checkpoint.parent,
                    {"event": "http_task_list", "checkpoint": str(checkpoint), "task_count": result.get("task_count")},
                )
                self.send_json(result)
                return
            if parsed.path == "/ui/tasks":
                form_token = params.get("token") if self.server.auth_token and params.get("token") == self.server.auth_token else None
                checkpoint = Path(str(params["checkpoint"])) if params.get("checkpoint") else None
                self.send_html(render_tasks_html(self.server.root, checkpoint=checkpoint, form_auth_token=form_token))
                return
            if parsed.path == "/workers":
                active_window_s = int(params.get("active_window_s", 600))
                self.send_json(list_workers(self.server.root, active_window_s=active_window_s))
                return
            self.send_error_json(f"unknown endpoint: {parsed.path}", status=404)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                raw = self.rfile.read(int(self.headers.get("Content-Length") or "0")).decode("utf-8")
                body = {key: values[-1] for key, values in parse_qs(raw).items()}
            else:
                body = read_json_body(self)

            if not self.authorized(body=body):
                self.send_auth_error()
                return

            if parsed.path == "/memory/append":
                record = body.get("record") if isinstance(body.get("record"), dict) else {
                    key: value for key, value in body.items() if key != "auth_token"
                }
                record = {"schema_version": "lockagent.global_memory.v0.1", "timestamp": timestamp(), **record}
                append_record(self.server.memory, record)
                result = {
                    "schema_version": SCHEMA_VERSION,
                    "status": "appended",
                    "memory": str(self.server.memory),
                    "record": record,
                    "count": len(read_jsonl(self.server.memory)),
                }
                append_service_event(self.server.root, {"event": "http_memory_append", "memory": str(self.server.memory), "count": result["count"]})
                self.send_json(result)
                return

            if parsed.path == "/workers/register":
                result = register_worker(self.server.root, body)
                append_service_event(
                    self.server.root,
                    {"event": "http_worker_register", "worker_id": result.get("worker", {}).get("worker_id"), "status": result.get("status")},
                )
                self.send_json(result)
                return

            if parsed.path == "/workers/heartbeat":
                result = heartbeat_registered_worker(self.server.root, body)
                append_service_event(
                    self.server.root,
                    {"event": "http_worker_registry_heartbeat", "worker_id": result.get("worker", {}).get("worker_id"), "status": result.get("status")},
                )
                self.send_json(result)
                return

            if parsed.path == "/hitl/approve":
                checkpoint = Path(str(body["checkpoint"]))
                result = hitl_approve(
                    checkpoint,
                    approved_by=str(body.get("approved_by", "runtime-service")),
                    note=str(body.get("note", "")),
                    auto_resume=as_bool(body.get("auto_resume", False)),
                    dry_run=as_bool(body.get("dry_run", False)),
                )
                append_service_event(
                    checkpoint.parent,
                    {"event": "http_hitl_approve", "checkpoint": str(checkpoint), "status": result.get("approval", {}).get("status")},
                )
                if self.headers.get("Content-Type", "").startswith("application/x-www-form-urlencoded"):
                    self.send_html("<html><body><pre>" + html.escape(stable_json(result)) + "</pre><a href='/ui/hitl'>Back</a></body></html>")
                else:
                    self.send_json(result)
                return

            if parsed.path == "/resume":
                checkpoint = Path(str(body["checkpoint"]))
                result = RuntimeReplayExecutor(
                    checkpoint,
                    cwd=Path(body["cwd"]) if body.get("cwd") else None,
                    dry_run=as_bool(body.get("dry_run", False)),
                ).replay(
                    from_step=body.get("from_step"),
                    only_failed=not as_bool(body.get("all_from_step", False)),
                    drain_queue=as_bool(body.get("drain_queue", False)),
                )
                append_service_event(
                    checkpoint.parent,
                    {"event": "http_resume", "checkpoint": str(checkpoint), "resume_status": result.get("status")},
                )
                self.send_json(result, status=200 if result.get("status") in {"ok", "nothing_to_resume"} else 409)
                return

            if parsed.path == "/tasks/submit":
                checkpoint = Path(str(body["checkpoint"]))
                result = task_submit(
                    checkpoint,
                    task_id=str(body.get("task_id") or f"http-task-{int(time.time() * 1000)}"),
                    role=str(body["role"]),
                    step_name=str(body["step_name"]),
                    payload=body.get("payload") if isinstance(body.get("payload"), dict) else {},
                    priority=int(body.get("priority", 100)),
                    submitted_by=str(body.get("submitted_by", "runtime-service-http")),
                )
                append_service_event(
                    checkpoint.parent,
                    {
                        "event": "http_task_submit",
                        "checkpoint": str(checkpoint),
                        "task_id": result.get("task", {}).get("task_id"),
                        "role": result.get("task", {}).get("role"),
                    },
                )
                self.send_json(result)
                return

            if parsed.path == "/worker/once":
                checkpoint = Path(str(body["checkpoint"]))
                roles = body.get("roles")
                if isinstance(roles, str):
                    roles = [roles]
                result = worker_once(
                    checkpoint,
                    roles=roles if isinstance(roles, list) else None,
                    dry_run=as_bool(body.get("dry_run", False)),
                    worker_id=str(body.get("worker_id", "http-worker")),
                    lease_s=int(body.get("lease_s", 300)),
                )
                append_service_event(
                    checkpoint.parent,
                    {"event": "http_worker_once", "checkpoint": str(checkpoint), "worker_status": result.get("status"), "worker_id": result.get("worker_id")},
                )
                self.send_json(result, status=200 if result.get("status") in {"ok", "skipped", "no_task"} else 500)
                return

            if parsed.path == "/worker/lease":
                checkpoint = Path(str(body["checkpoint"]))
                roles = body.get("roles")
                if isinstance(roles, str):
                    roles = [roles]
                result = worker_lease(
                    checkpoint,
                    roles=roles if isinstance(roles, list) else None,
                    worker_id=str(body.get("worker_id", "remote-worker")),
                    lease_s=int(body.get("lease_s", 300)),
                )
                append_service_event(
                    checkpoint.parent,
                    {"event": "http_worker_lease", "checkpoint": str(checkpoint), "worker_status": result.get("status"), "worker_id": result.get("worker_id")},
                )
                self.send_json(result, status=200 if result.get("status") in {"leased", "no_task"} else 500)
                return

            if parsed.path == "/worker/complete":
                checkpoint = Path(str(body["checkpoint"]))
                result = worker_complete(
                    checkpoint,
                    task_id=str(body["task_id"]),
                    worker_id=str(body["worker_id"]),
                    status=str(body["status"]),
                    returncode=int(body["returncode"]) if body.get("returncode") is not None else None,
                    result_payload=body.get("result") if isinstance(body.get("result"), dict) else {},
                )
                append_service_event(
                    checkpoint.parent,
                    {"event": "http_worker_complete", "checkpoint": str(checkpoint), "ok": result.get("ok"), "worker_id": result.get("worker_id"), "task_id": body.get("task_id")},
                )
                self.send_json(result, status=200 if result.get("ok") else 409)
                return

            if parsed.path == "/worker/heartbeat":
                checkpoint = Path(str(body["checkpoint"]))
                result = worker_heartbeat(
                    checkpoint,
                    task_id=str(body["task_id"]),
                    worker_id=str(body["worker_id"]),
                    lease_s=int(body.get("lease_s", 300)),
                )
                append_service_event(
                    checkpoint.parent,
                    {"event": "http_worker_heartbeat", "checkpoint": str(checkpoint), "ok": result.get("ok"), "worker_id": result.get("worker_id")},
                )
                self.send_json(result, status=200 if result.get("ok") else 409)
                return

            if parsed.path == "/adapter/invoke":
                result = invoke_from_body(body)
                checkpoint = Path(str(body["checkpoint"]))
                append_service_event(
                    checkpoint.parent,
                    {
                        "event": "http_adapter_invoke",
                        "checkpoint": str(checkpoint),
                        "framework": result.get("framework"),
                        "invocation_type": result.get("invocation_type"),
                        "adapter_status": result.get("status"),
                    },
                )
                self.send_json(result, status=200 if result.get("status") in {"submitted", "delivered", "ok", "nothing_to_resume"} else 409)
                return

            self.send_error_json(f"unknown endpoint: {parsed.path}", status=404)
        except KeyError as exc:
            self.send_error_json(f"missing field: {exc}", status=400)
        except Exception as exc:
            self.send_error_json(str(exc), status=500)


class LockAgentHTTPServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        handler_class: type[BaseHTTPRequestHandler],
        *,
        root: Path,
        memory: Path,
        auth_token: str | None = None,
    ) -> None:
        super().__init__(server_address, handler_class)
        self.root = root
        self.memory = memory
        self.auth_token = auth_token


def serve(*, host: str, port: int, root: Path, memory: Path, auth_token: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    memory.parent.mkdir(parents=True, exist_ok=True)
    server = LockAgentHTTPServer((host, port), LockAgentRequestHandler, root=root, memory=memory, auth_token=auth_token)
    print(
        stable_json(
            {
                "schema_version": SCHEMA_VERSION,
                "status": "serving",
                "host": host,
                "port": port,
                "root": str(root),
                "memory": str(memory),
                "auth_enabled": bool(auth_token),
            }
        )
    )
    server.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--root", type=Path, default=Path("results"))
    ap.add_argument("--memory", type=Path, default=Path("results/memory/global_memory.jsonl"))
    ap.add_argument("--auth-token")
    args = ap.parse_args(argv)
    serve(host=args.host, port=args.port, root=args.root, memory=args.memory, auth_token=args.auth_token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
