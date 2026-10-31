#!/usr/bin/env python3
"""把 FlowCausal v2 最终各组实验汇总到一份可复查记录。"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parents[1]
RESULTS = ROOT / "experiments" / "results"
CANDIDATE = "flowcausal_v2_automatic_weight"
METRICS = (
    ("makespan_sec", "最后完成"),
    ("mean_workflow_completion_sec", "平均工作流"),
    ("p95_workflow_completion_sec", "工作流 95 分位"),
    ("p95_synchronous_tool_service_sec", "同步工具 95 分位"),
)
SUITES = (
    ("main", "混合四工作流", "v2-final-automatic-recent-cpu-main-{tag}"),
    ("return", "返回关系四工作流", "v2-final-automatic-recent-cpu-return-{tag}"),
    ("build", "构建后继续执行四工作流", "v2-final-automatic-recent-cpu-build-{tag}"),
    ("short", "短后续进程四工作流", "v2-final-automatic-recent-cpu-short-{tag}"),
    ("cpu4", "返回关系四工作流，4 个处理器", "v2-final-automatic-recent-cpu-cpu4-{tag}"),
    ("cpu16", "返回关系四工作流，16 个处理器", "v2-final-automatic-recent-cpu-cpu16-{tag}"),
    ("eight", "返回关系八工作流，8 个处理器", "v2-final-automatic-recent-cpu-8workflow-{tag}"),
)


def load(path: pathlib.Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SystemExit(f"结果不是 JSON 对象：{path}")
    return value


def fmt(stat: dict[str, Any]) -> str:
    return f"{float(stat['mean']):.3f}±{float(stat['sample_stdev']):.3f}"


def comparison_fmt(item: dict[str, Any]) -> str:
    return (
        f"{float(item['reduction_percent']):.2f}% "
        f"[{float(item['welch_95_low']):.2f}%, "
        f"{float(item['welch_95_high']):.2f}%]"
    )


def render_suite(lines: list[str], title: str, payload: dict[str, Any]) -> None:
    candidate = str(payload["candidate"])
    references = [str(item) for item in payload["references"]]
    methods = payload["methods"]
    lines.extend(
        [
            f"## {title}",
            "",
            "| 方法 | 轮数 | 最后完成（秒） | 平均工作流（秒） | 工作流 95 分位（秒） | 同步工具 95 分位（秒） |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for method in [*references, candidate]:
        stats = methods[method]
        count = int(stats[METRICS[0][0]]["count"])
        values = " | ".join(fmt(stats[field]) for field, _ in METRICS)
        lines.append(f"| `{method}` | {count} | {values} |")

    lines.extend(
        [
            "",
            f"### `{candidate}` 相对其他方法的变化",
            "",
            "正数表示时间缩短；方括号内是独立样本均值差的 95% 置信区间。",
            "",
            "| 方法 | 最后完成 | 平均工作流 | 工作流 95 分位 | 同步工具 95 分位 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    comparisons = payload["candidate_reduction_percent"]
    for method in references:
        values = " | ".join(
            comparison_fmt(comparisons[method][field]) for field, _ in METRICS
        )
        lines.append(f"| `{method}` | {values} |")

    lines.extend(
        [
            "",
            "### 逐工作流完成时间",
            "",
            "| 方法 | 工作流 | 均值（秒） | 样本标准差（秒） |",
            "|---|---|---:|---:|",
        ]
    )
    workflows = payload["workflows"]
    for method in [*references, candidate]:
        for instance, values in workflows[method].items():
            stat = values["workflow_completion_sec"]
            lines.append(
                f"| `{method}` | `{instance}` | {float(stat['mean']):.3f} | "
                f"{float(stat['sample_stdev']):.3f} |"
            )
    lines.append("")


def main() -> int:
    parser = argparse.ArgumentParser(description="汇总 FlowCausal v2 最终实验")
    parser.add_argument("--tag", default="20260914-service-deficit-v1")
    parser.add_argument(
        "--output",
        type=pathlib.Path,
        default=RESULTS / "v2-final-closure-20260914",
    )
    args = parser.parse_args()

    records: dict[str, Any] = {}
    sources: dict[str, str] = {}
    missing: list[str] = []
    for key, title, pattern in SUITES:
        directory = RESULTS / pattern.format(tag=args.tag)
        source = directory / "统计-与历史同条件结果比较" / "统计结果.json"
        if not source.exists():
            missing.append(str(source.relative_to(ROOT)))
            continue
        payload = load(source)
        if payload.get("candidate") != CANDIDATE:
            raise SystemExit(f"{source} 的候选方法不是 {CANDIDATE}")
        records[key] = {"title": title, "result": payload}
        sources[key] = str(source.relative_to(ROOT))
    if missing:
        raise SystemExit("以下结果尚未完成：\n" + "\n".join(missing))

    result = {
        "schema": "flowcausal.v2.closure-summary.v1",
        "candidate": CANDIDATE,
        "comparison_kind": "independent_samples",
        "sources": sources,
        "suites": records,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "完整结果.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    lines = [
        "# FlowCausal v2 最终实验完整记录",
        "",
        "本文件由原始 JSON 自动生成。最新版只运行一次符合要求的实验，历史方法读取其已经保存的同条件结果。由于两组数据不是同一批交错运行，变化区间按独立样本计算，不能解释为配对实验。",
        "",
    ]
    for key, title, _ in SUITES:
        render_suite(lines, title, records[key]["result"])
    lines.extend(
        [
            "## 原始记录",
            "",
            *[f"- {records[key]['title']}：`{sources[key]}`" for key, _, _ in SUITES],
            "",
        ]
    )
    (args.output / "完整结果.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(args.output / "完整结果.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
