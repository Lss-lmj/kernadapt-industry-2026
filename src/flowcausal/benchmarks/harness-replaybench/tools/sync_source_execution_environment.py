#!/usr/bin/env python3
"""把来源智能体的工具执行环境同步到 HarnessReplayBench 1.0 轨迹。"""
from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]


def read_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: pathlib.Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def source_environment(raw: dict[str, Any]) -> dict[str, Any]:
    info = raw.get("info") if isinstance(raw.get("info"), dict) else {}
    config = info.get("config") if isinstance(info.get("config"), dict) else {}
    environment = config.get("environment") if isinstance(config.get("environment"), dict) else {}
    result: dict[str, Any] = {}
    interpreter = environment.get("interpreter")
    variables = environment.get("env")
    if isinstance(interpreter, list) and interpreter and all(
        isinstance(item, str) and item for item in interpreter
    ):
        result["interpreter"] = interpreter
    if isinstance(variables, dict) and all(
        isinstance(key, str) and isinstance(value, str) for key, value in variables.items()
    ):
        result["env"] = variables
    return result


def source_index(source_root: pathlib.Path) -> dict[str, pathlib.Path]:
    """按文件名索引全部来源记录，并拒绝无法唯一定位的记录。"""
    candidates: dict[str, list[pathlib.Path]] = {}
    for path in sorted(item for item in source_root.rglob("*") if item.is_file()):
        candidates.setdefault(path.name, []).append(path)
    ambiguous = {
        name: paths for name, paths in candidates.items() if len(paths) > 1
    }
    if ambiguous:
        details = "; ".join(
            f"{name}: {', '.join(str(path) for path in paths)}"
            for name, paths in sorted(ambiguous.items())
        )
        raise SystemExit(f"来源记录文件名不唯一：{details}")
    return {name: paths[0] for name, paths in candidates.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description="同步真实采集时的工具命令环境")
    parser.add_argument("--trace-root", default=str(ROOT / "traces" / "formal"))
    parser.add_argument("--source-root", default=str(ROOT / "source-artifacts"))
    args = parser.parse_args()
    trace_root = pathlib.Path(args.trace_root).resolve()
    source_root = pathlib.Path(args.source_root).resolve()
    sources = source_index(source_root)
    updated: list[str] = []
    without_recorded_environment: list[str] = []
    missing_source: list[str] = []
    for trace_path in sorted(trace_root.glob("*.json")):
        trace = read_json(trace_path)
        capture = trace.get("capture") if isinstance(trace.get("capture"), dict) else {}
        source_name = str(capture.get("source_trajectory") or "")
        source_path = sources.get(pathlib.Path(source_name).name) if source_name else None
        if source_path is None:
            missing_source.append(trace_path.name)
            continue
        environment = source_environment(read_json(source_path))
        if not environment:
            without_recorded_environment.append(trace_path.name)
            continue
        workspace = trace.get("workspace")
        if not isinstance(workspace, dict):
            raise SystemExit(f"{trace_path} 缺少 workspace")
        workspace["command_environment"] = environment
        write_json(trace_path, trace)
        updated.append(trace_path.name)
    print(
        json.dumps(
            {
                "updated": updated,
                "without_recorded_environment": without_recorded_environment,
                "missing_source": missing_source,
            },
            ensure_ascii=False,
        )
    )
    if missing_source:
        raise SystemExit("存在无法定位来源记录的正式轨迹")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
