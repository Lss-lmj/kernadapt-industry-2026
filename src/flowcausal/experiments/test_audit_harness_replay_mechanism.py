#!/usr/bin/env python3
"""Tests for HarnessReplayBench mechanism audit."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest


HERE = pathlib.Path(__file__).resolve().parent
AUDIT = HERE / "audit_harness_replay_mechanism.py"


def load_audit_module():
    spec = importlib.util.spec_from_file_location("audit_harness_replay_mechanism", AUDIT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {AUDIT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


audit_mod = load_audit_module()


FLOWMON_TEXT = """
== FlowCausal snapshot ==
FlowCausal policies:
  session=1 flags=[enable] min_conf=75 width=1 boost=500000 window=100000000 budget=10/100 expires=999 anchor_cpu=-1
FlowCausal stats:
  session=1 enq=10 frontier=3 fallback=2 width_held=1 inflight=0 width_releases=1 frontier_ratio=0.3 fallback_ratio=0.2
    wait_ns total=1000 frontier=400 runtime_ns total=2000 frontier=700 last_event=1
FlowCausal global stats:
  dispatch frontier=3 shared=7 shared_first=0 guard_trips=0 guard_misses=0 cooldown_entries=0 cooldown_dispatches=0 cooldown_misses=0 pressure_fallback_entries=0 pressure_fallback_enqueues=0 shared_wait_pressure_events=0 shared_wait_pressure_wait_ns=0 pressure_dispatch_entries=0 pressure_dispatches=0 pressure_dispatch_misses=0 shared_wakeup_preempt_entries=0 shared_wakeup_preempt_dispatches=0 shared_wakeup_preempt_misses=0 shared_local_enqueues=0 shared_percpu_enqueues=0 shared_percpu_dispatches=0 shared_percpu_misses=0 frontier_wakeup_preempt_entries=0 background_enqueues=0 background_dispatches=0 frontier_budget_windows=0 frontier_budget_trips=0 frontier_budget_shared=0 frontier_budget_misses=0
"""


class HarnessReplayMechanismAuditTests(unittest.TestCase):
    def test_cli_audits_flowcausal_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trial = tmp / "runs" / "flow" / "trial-01"
            case_dir = trial / "runs" / "case-a"
            live_dir = trial / "flowcausal-live"
            case_dir.mkdir(parents=True)
            live_dir.mkdir(parents=True)
            (case_dir / "controller.ndjson").write_text(
                json.dumps(
                    {
                        "type": "flowctl_watch",
                        "stage": "TEST",
                        "stage_source": "state-file",
                        "frontier_action": {
                            "action": "advance",
                            "reason": "requested-advance",
                            "build_frontier_admission": "tail-only",
                        },
                        "frontiers": [
                            {
                                "pid": 123,
                                "comm": "python",
                                "score": 215,
                                "reason": "test worker,state=R,parent-wait:python,barrier-tail:1",
                            }
                        ],
                        "propagation_anchors": [{"pid": 100, "comm": "python"}],
                    }
                )
                + "\n"
                + json.dumps(
                    {
                        "type": "flowctl_watch",
                        "stage": "TEST",
                        "stage_source": "state-file",
                        "frontier_action": {
                            "action": "observe",
                            "reason": "weak-test-worker-frontier:selected=1,max_tail=1",
                            "policy_fallback": True,
                            "build_frontier_admission": "tail-only",
                        },
                        "frontiers": [
                            {
                                "pid": 124,
                                "comm": "python",
                                "score": 215,
                                "reason": "test worker,state=R,parent-wait:python,barrier-tail:1",
                            }
                        ],
                        "propagation_anchors": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            (live_dir / "flowmon-final.log").write_text(FLOWMON_TEXT, encoding="utf-8")
            matrix = {
                "schema": "flowcausal.harness_replay_matrix.v1",
                "status": "pass",
                "variants": [
                    {
                        "name": "default",
                        "kind": "baseline",
                        "summary": {"pass_trials": 1, "trials": 1},
                        "comparison_to_baseline": {
                            "makespan_delta": 0.0,
                            "mean_critical_delta": 0.0,
                            "p95_critical_delta": 0.0,
                        },
                        "trials": [
                            {
                                "trial": 1,
                                "status": "pass",
                                "run_dir": str(tmp / "runs" / "default" / "trial-01"),
                                "fidelity": {"status": "pass"},
                                "aggregate": {"mean_critical_sec": 10.0},
                                "cases": [],
                            }
                        ],
                    },
                    {
                        "name": "flowcausal_case_width2",
                        "kind": "flowcausal",
                        "variant_options": {
                            "kind": "flowcausal",
                            "per_case_flowctl": "true",
                            "width_control": "true",
                            "frontier_width": "2",
                            "max_frontier_width": "2",
                        },
                        "summary": {"pass_trials": 1, "trials": 1},
                        "comparison_to_baseline": {
                            "makespan_delta": -0.05,
                            "mean_critical_delta": -0.07,
                            "p95_critical_delta": -0.01,
                        },
                        "trials": [
                            {
                                "trial": 1,
                                "status": "pass",
                                "run_dir": str(trial),
                                "fidelity": {"status": "pass"},
                                "per_case_flowctl": True,
                                "flowcausal_live": {
                                    "flowmon_final_log": str(live_dir / "flowmon-final.log"),
                                    "manifest": {
                                        "scheduler_returncode": 0,
                                        "flowmon_final_returncode": 0,
                                        "sched_ext_state_after": "disabled",
                                    },
                                },
                                "aggregate": {"mean_critical_sec": 9.0},
                                "cases": [
                                    {
                                        "instance_id": "case-a",
                                        "controller": {
                                            "status": "stopped_after_workload",
                                            "returncode": 130,
                                            "root_pid": 123,
                                            "json_log": str(case_dir / "controller.ndjson"),
                                        },
                                    }
                                ],
                            }
                        ],
                    },
                ],
            }
            matrix_path = tmp / "matrix.json"
            output = tmp / "audit.json"
            markdown = tmp / "audit.md"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(AUDIT),
                    "--matrix-summary",
                    str(matrix_path),
                    "--output",
                    str(output),
                    "--markdown-output",
                    str(markdown),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(data["schema"], "flowcausal.harness_replay_mechanism_audit.v1")
            by_name = {variant["name"]: variant for variant in data["variants"]}
            self.assertEqual(by_name["default"]["classification"], "baseline")
            flow = by_name["flowcausal_case_width2"]
            self.assertEqual(flow["status"], "pass")
            self.assertEqual(flow["classification"], "candidate-with-width-risk")
            self.assertEqual(flow["mechanism_summary"]["frontier_enqueues_mean"], 3.0)
            self.assertEqual(flow["mechanism_summary"]["width_held_enqueues_mean"], 1.0)
            self.assertGreater(flow["mechanism_summary"]["controller_frontier_records_mean"], 0)
            self.assertEqual(
                flow["mechanism_summary"]["controller_frontier_action_counts"],
                {"advance": 1, "observe": 1},
            )
            self.assertEqual(
                flow["mechanism_summary"]["controller_frontier_action_reason_group_counts"],
                {"requested-advance": 1, "weak-test-worker-frontier": 1},
            )
            self.assertEqual(
                flow["mechanism_summary"]["controller_frontier_action_observe_ratio_mean"],
                0.5,
            )
            self.assertEqual(
                flow["mechanism_summary"]["controller_weak_test_worker_frontier_records_mean"],
                1.0,
            )
            self.assertTrue(markdown.is_file())
            markdown_text = markdown.read_text(encoding="utf-8")
            self.assertIn("advance=1, observe=1", markdown_text)
            self.assertIn("weak-test-worker-frontier=1", markdown_text)

    def test_cli_treats_no_controller_as_overhead_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            tmp = pathlib.Path(raw_tmp)
            trial = tmp / "runs" / "flowcausal_attached" / "trial-01"
            live_dir = trial / "flowcausal-live"
            live_dir.mkdir(parents=True)
            (live_dir / "flowmon-final.log").write_text(
                """
== FlowCausal snapshot ==
FlowCausal policies:
FlowCausal stats:
FlowCausal global stats:
  dispatch frontier=0 shared=100 shared_first=0 guard_trips=0 guard_misses=0 cooldown_entries=0 cooldown_dispatches=0 cooldown_misses=0 pressure_fallback_entries=0 pressure_fallback_enqueues=0 shared_wait_pressure_events=0 shared_wait_pressure_wait_ns=0 pressure_dispatch_entries=0 pressure_dispatches=0 pressure_dispatch_misses=0 shared_wakeup_preempt_entries=0 shared_wakeup_preempt_dispatches=0 shared_wakeup_preempt_misses=0 shared_local_enqueues=0 shared_percpu_enqueues=0 shared_percpu_dispatches=0 shared_percpu_misses=0 frontier_wakeup_preempt_entries=0 background_enqueues=0 background_dispatches=0 frontier_budget_windows=0 frontier_budget_trips=0 frontier_budget_shared=0 frontier_budget_misses=0
""",
                encoding="utf-8",
            )
            matrix = {
                "schema": "flowcausal.harness_replay_matrix.v1",
                "status": "pass",
                "variants": [
                    {
                        "name": "flowcausal_attached",
                        "kind": "flowcausal",
                        "variant_options": {"kind": "flowcausal", "no_controller": "true"},
                        "summary": {"pass_trials": 1, "trials": 1},
                        "comparison_to_baseline": {
                            "makespan_delta": 0.04,
                            "mean_critical_delta": 0.02,
                            "p95_critical_delta": -0.01,
                        },
                        "trials": [
                            {
                                "trial": 1,
                                "status": "pass",
                                "run_dir": str(trial),
                                "fidelity": {"status": "pass"},
                                "flowcausal_live": {
                                    "flowmon_final_log": str(live_dir / "flowmon-final.log"),
                                    "manifest": {
                                        "controller_enabled": False,
                                        "controller_returncode": None,
                                        "scheduler_returncode": 0,
                                        "flowmon_final_returncode": 0,
                                        "sched_ext_state_after": "disabled",
                                    },
                                },
                                "aggregate": {"mean_critical_sec": 10.0},
                                "cases": [],
                            }
                        ],
                    }
                ],
            }
            matrix_path = tmp / "matrix.json"
            output = tmp / "audit.json"
            matrix_path.write_text(json.dumps(matrix), encoding="utf-8")

            proc = subprocess.run(
                [
                    sys.executable,
                    str(AUDIT),
                    "--matrix-summary",
                    str(matrix_path),
                    "--output",
                    str(output),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            data = json.loads(output.read_text(encoding="utf-8"))
            variant = data["variants"][0]
            self.assertEqual(variant["status"], "pass")
            self.assertEqual(variant["classification"], "overhead-baseline")
            self.assertEqual(variant["mechanism_summary"]["overhead_baseline_trials"], 1)
            self.assertEqual(variant["mechanism_summary"]["frontier_enqueues_mean"], 0.0)


if __name__ == "__main__":
    unittest.main()
