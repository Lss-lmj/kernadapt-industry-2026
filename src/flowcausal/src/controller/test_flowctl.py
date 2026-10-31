#!/usr/bin/env python3
"""Small regression tests for FlowCausal's user-space frontier logic."""

from __future__ import annotations

import dataclasses
import json
import pathlib
import tempfile
import unittest
from unittest import mock

import flowctl


def proc(
    pid: int,
    ppid: int,
    state: str,
    comm: str,
    command: str,
    *,
    start_time: int = 0,
    cpu_ticks: int = 0,
    children: tuple[int, ...] = (),
) -> flowctl.ProcInfo:
    return flowctl.ProcInfo(
        pid=pid,
        ppid=ppid,
        state=state,
        start_time=start_time,
        cpu_ticks=cpu_ticks,
        comm=comm,
        command=command,
        child_count=len(children),
        child_pids=children,
    )


class FrontierRankingTests(unittest.TestCase):
    def test_sleeping_build_driver_waiting_on_cpu_leaf_is_not_frontier(self) -> None:
        make = proc(
            100,
            1,
            "S",
            "make",
            "make -j2 all",
            start_time=0,
            children=(101,),
        )
        cc1 = proc(
            101,
            100,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=1,
            cpu_ticks=10,
        )

        ranked = flowctl.rank_frontier_candidates([make, cc1], flowctl.Stage.BUILD)

        self.assertEqual(ranked[0][1], 101)
        self.assertIn("parent-wait:make", ranked[0][2])
        make_entry = next(item for item in ranked if item[1] == 100)
        self.assertIn("waiting-parent", make_entry[2])

    def test_runnable_build_driver_remains_candidate_without_serviceable_child(self) -> None:
        make = proc(
            200,
            1,
            "R",
            "make",
            "make -j2 all",
            start_time=0,
            cpu_ticks=3,
        )

        ranked = flowctl.rank_frontier_candidates([make], flowctl.Stage.BUILD)

        self.assertEqual(ranked[0][1], 200)
        self.assertGreaterEqual(ranked[0][0], flowctl.MIN_FRONTIER_SCORE)
        self.assertIn("build driver", ranked[0][2])

    def test_build_stage_downranks_generic_background_when_compiler_exists(self) -> None:
        bash = proc(
            230,
            1,
            "S",
            "bash",
            "bash -lc make",
            start_time=0,
            children=(231, 232),
        )
        noise = proc(
            231,
            230,
            "R",
            "python3",
            "python3 cpu_noise.py",
            start_time=1,
            cpu_ticks=20,
        )
        cc1 = proc(
            232,
            230,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=2,
            cpu_ticks=10,
        )

        ranked = flowctl.rank_frontier_candidates([bash, noise, cc1], flowctl.Stage.BUILD)

        self.assertEqual(ranked[0][1], 232)
        noise_entry = next(item for item in ranked if item[1] == 231)
        self.assertIn("stage-generic-background", noise_entry[2])
        self.assertLess(noise_entry[0], flowctl.MIN_FRONTIER_SCORE)

    def test_stage_state_file_overrides_command_inference_for_live_repo_stage(self) -> None:
        root = proc(
            250,
            1,
            "S",
            "python3",
            "python3 repo_stage_workflow.py",
            start_time=0,
            children=(251,),
        )
        pytest = proc(
            251,
            250,
            "R",
            "pytest",
            "pytest tests",
            start_time=1,
            cpu_ticks=20,
        )

        with tempfile.TemporaryDirectory() as raw_tmp:
            state_file = pathlib.Path(raw_tmp) / "stage-state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "type": "flowcausal_stage_state",
                        "stage": "BUILD",
                        "phase": "begin",
                        "turn": 0,
                    }
                ),
                encoding="utf-8",
            )
            with (
                mock.patch.object(flowctl, "subtree", return_value=[250, 251]),
                mock.patch.object(flowctl, "collect_proc_info", return_value={250: root, 251: pytest}),
            ):
                plan = flowctl.build_hint_plan(
                    250,
                    "session:stage-state",
                    75,
                    -1,
                    1000,
                    100,
                    10,
                    500,
                    True,
                    1,
                    1,
                    flowctl.MIN_FRONTIER_SCORE,
                    False,
                    0,
                    False,
                    None,
                    stage_state_file=state_file,
                )

        self.assertEqual(plan.stage, flowctl.Stage.BUILD)
        self.assertEqual(plan.stage_source, "state-file")
        self.assertEqual(plan.stage_label, "BUILD")

    def test_stage_state_file_accepts_workload_stage_aliases(self) -> None:
        with tempfile.TemporaryDirectory() as raw_tmp:
            state_file = pathlib.Path(raw_tmp) / "stage-state.json"
            state_file.write_text(
                json.dumps({"stage": "RETRY_PROBE", "phase": "begin"}),
                encoding="utf-8",
            )

            stage, record = flowctl.parse_stage_state_file(state_file)

        self.assertEqual(stage, flowctl.Stage.RETRY)
        self.assertEqual(record["stage"], "RETRY_PROBE")

    def test_build_plan_marks_stage_generic_background_hint(self) -> None:
        root = proc(
            260,
            1,
            "S",
            "bash",
            "bash -lc make",
            start_time=0,
            children=(261, 262),
        )
        noise = proc(
            261,
            260,
            "R",
            "python3",
            "python3 cpu_noise.py",
            start_time=1,
            cpu_ticks=20,
        )
        cc1 = proc(
            262,
            260,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=2,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[260, 261, 262]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={260: root, 261: noise, 262: cc1}),
        ):
            plan = flowctl.build_hint_plan(
                260,
                "session:background",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.BUILD,
            )

        hints = {hint.pid: hint for hint in plan.hints}
        self.assertTrue(hints[261].flags & flowctl.TASK_BACKGROUND)
        self.assertFalse(hints[262].flags & flowctl.TASK_BACKGROUND)

    def test_build_plan_never_promotes_stage_generic_background_as_frontier(self) -> None:
        root = proc(
            270,
            1,
            "S",
            "bash",
            "bash -lc make paper",
            start_time=0,
            children=(271,),
        )
        make = proc(
            271,
            270,
            "S",
            "make",
            "make -C flowcausal/paper eurosys",
            start_time=1,
            children=(272,),
        )
        pdflatex = proc(
            272,
            271,
            "R",
            "pdflatex",
            "pdflatex -interaction=nonstopmode paper_eurosys.tex",
            start_time=2,
            cpu_ticks=20,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[270, 271, 272]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={270: root, 271: make, 272: pdflatex}),
        ):
            plan = flowctl.build_hint_plan(
                270,
                "session:paper-background",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.BUILD,
            )

        hints = {hint.pid: hint for hint in plan.hints}
        ranked_pdflatex = next(item for item in plan.ranked_frontiers if item[1] == 272)
        self.assertIn("stage-generic-background", ranked_pdflatex[2])
        self.assertTrue(hints[272].flags & flowctl.TASK_BACKGROUND)
        self.assertFalse(hints[272].flags & flowctl.TASK_FRONTIER)
        self.assertNotIn(272, plan.frontier_pids)

    def test_harness_make_jobs_option_is_not_misclassified_as_make(self) -> None:
        runner = proc(
            250,
            1,
            "S",
            "python3",
            "python3 scripts/run_agent_sandbox_sched.py --workloads shell_mix --make-jobs 1",
            start_time=0,
            cpu_ticks=3,
        )

        self.assertEqual(flowctl.infer_stage_from_infos([runner]), flowctl.Stage.SHELL)
        ranked = flowctl.rank_frontier_candidates([runner], flowctl.Stage.SHELL)

        self.assertEqual(ranked[0][1], 250)
        self.assertLess(ranked[0][0], flowctl.MIN_FRONTIER_SCORE)
        self.assertIn("generic task", ranked[0][2])
        self.assertNotIn("build driver", ranked[0][2])


class HintRefreshTests(unittest.TestCase):
    def test_task_refresh_drops_old_semantic_flags_but_keeps_accounting(self) -> None:
        old = flowctl.TaskHint(
            pid=300,
            tgid=300,
            parent_pid=1,
            session_id=11,
            stage_id=22,
            cohort_id=33,
            flags=flowctl.TASK_FRONTIER | flowctl.TASK_COHORT_TAIL | flowctl.TASK_PROPAGATE,
            confidence=90,
            enqueue_ts_ns=1000,
            running_since_ns=2000,
            runnable_wait_ns=3000,
            runtime_ns=4000,
            last_update_ns=5000,
        )
        new = flowctl.TaskHint(
            pid=300,
            tgid=300,
            parent_pid=1,
            session_id=11,
            stage_id=22,
            cohort_id=33,
            flags=0,
            confidence=0,
        )

        with mock.patch.object(flowctl, "bpftool_lookup", return_value=old.pack_value()):
            merged = flowctl.merge_existing_task_hint(object(), new)

        self.assertEqual(merged.flags, 0)
        self.assertEqual(merged.confidence, 0)
        self.assertEqual(merged.enqueue_ts_ns, old.enqueue_ts_ns)
        self.assertEqual(merged.running_since_ns, old.running_since_ns)
        self.assertEqual(merged.runnable_wait_ns, old.runnable_wait_ns)
        self.assertEqual(merged.runtime_ns, old.runtime_ns)
        self.assertEqual(merged.last_update_ns, old.last_update_ns)

    def test_noop_hint_is_not_effective(self) -> None:
        hint = flowctl.TaskHint(
            pid=310,
            tgid=310,
            parent_pid=1,
            session_id=11,
            stage_id=22,
            cohort_id=33,
            flags=0,
            confidence=0,
        )

        self.assertFalse(flowctl.is_effective_task_hint(hint))
        self.assertTrue(
            flowctl.is_effective_task_hint(
                dataclasses.replace(hint, flags=flowctl.TASK_BACKGROUND)
            )
        )

    def test_update_maps_deletes_noop_task_hint_instead_of_tracking_it(self) -> None:
        policy = flowctl.SessionPolicy(session_id=123)
        noop = flowctl.TaskHint(
            pid=320,
            tgid=320,
            parent_pid=1,
            session_id=123,
            stage_id=22,
            cohort_id=33,
            flags=0,
            confidence=0,
        )
        effective = dataclasses.replace(noop, pid=321, flags=flowctl.TASK_PROPAGATE, confidence=75)

        with (
            mock.patch.object(flowctl, "merge_existing_policy", return_value=policy),
            mock.patch.object(flowctl, "merge_existing_task_hint", side_effect=lambda _map, hint: hint) as merge_task,
            mock.patch.object(flowctl, "bpftool_update") as update,
            mock.patch.object(flowctl, "bpftool_delete") as delete,
        ):
            flowctl.update_maps(pathlib.Path("/task"), pathlib.Path("/policy"), policy, [noop, effective])

        delete.assert_called_once_with("/task", noop.pack_key())
        merge_task.assert_called_once()
        updated_keys = [call.args[1] for call in update.call_args_list]
        self.assertIn(policy.pack_key(), updated_keys)
        self.assertIn(effective.pack_key(), updated_keys)
        self.assertNotIn(noop.pack_key(), updated_keys)

    def test_update_maps_skips_never_written_noop_hint_in_watch_mode(self) -> None:
        policy = flowctl.SessionPolicy(session_id=124)
        noop = flowctl.TaskHint(
            pid=330,
            tgid=330,
            parent_pid=1,
            session_id=124,
            stage_id=22,
            cohort_id=33,
            flags=0,
            confidence=0,
        )
        known: set[int] = set()

        with (
            mock.patch.object(flowctl, "merge_existing_policy", return_value=policy),
            mock.patch.object(flowctl, "bpftool_update") as update,
            mock.patch.object(flowctl, "bpftool_delete") as delete,
        ):
            flowctl.update_maps(pathlib.Path("/task"), pathlib.Path("/policy"), policy, [noop], known_task_pids=known)

        delete.assert_not_called()
        self.assertEqual(known, set())
        update.assert_not_called()

        known.add(noop.pid)
        with (
            mock.patch.object(flowctl, "merge_existing_policy", return_value=policy),
            mock.patch.object(flowctl, "bpftool_update"),
            mock.patch.object(flowctl, "bpftool_delete") as delete,
        ):
            flowctl.update_maps(pathlib.Path("/task"), pathlib.Path("/policy"), policy, [noop], known_task_pids=known)

        delete.assert_called_once_with("/task", noop.pack_key())
        self.assertEqual(known, set())

    def test_update_maps_force_delete_ignores_unknown_noop_hint_in_watch_mode(self) -> None:
        policy = flowctl.SessionPolicy(session_id=125)
        noop = flowctl.TaskHint(
            pid=340,
            tgid=340,
            parent_pid=1,
            session_id=125,
            stage_id=22,
            cohort_id=33,
            flags=0,
            confidence=0,
        )

        with (
            mock.patch.object(flowctl, "merge_existing_policy", return_value=policy),
            mock.patch.object(flowctl, "bpftool_update"),
            mock.patch.object(flowctl, "bpftool_delete") as delete,
        ):
            flowctl.update_maps(
                pathlib.Path("/task"),
                pathlib.Path("/policy"),
                policy,
                [noop],
                known_task_pids=set(),
                force_delete_pids={noop.pid},
            )

        delete.assert_not_called()

        known = {noop.pid}
        with (
            mock.patch.object(flowctl, "merge_existing_policy", return_value=policy),
            mock.patch.object(flowctl, "bpftool_update"),
            mock.patch.object(flowctl, "bpftool_delete") as delete,
        ):
            flowctl.update_maps(
                pathlib.Path("/task"),
                pathlib.Path("/policy"),
                policy,
                [noop],
                known_task_pids=known,
                force_delete_pids={noop.pid},
            )

        delete.assert_called_once_with("/task", noop.pack_key())
        self.assertEqual(known, set())


class PolicyFallbackTests(unittest.TestCase):
    def test_policy_refresh_can_clear_old_fallback_flag(self) -> None:
        old = flowctl.SessionPolicy(
            session_id=123,
            flags=flowctl.POLICY_ENABLE | flowctl.POLICY_FALLBACK,
            budget_used_ns=7_000,
            budget_window_start_ns=11_000,
        )
        new = flowctl.SessionPolicy(
            session_id=123,
            flags=flowctl.POLICY_ENABLE | flowctl.POLICY_WIDTH_CTL,
            budget_used_ns=0,
            budget_window_start_ns=0,
        )

        with mock.patch.object(flowctl, "bpftool_lookup", return_value=old.pack_value()):
            merged = flowctl.merge_existing_policy(object(), new)

        self.assertEqual(merged.flags, flowctl.POLICY_ENABLE | flowctl.POLICY_WIDTH_CTL)
        self.assertFalse(merged.flags & flowctl.POLICY_FALLBACK)
        self.assertEqual(merged.budget_used_ns, old.budget_used_ns)
        self.assertEqual(merged.budget_window_start_ns, old.budget_window_start_ns)

    def test_policy_refresh_keeps_current_fallback_flag(self) -> None:
        old = flowctl.SessionPolicy(
            session_id=124,
            flags=flowctl.POLICY_ENABLE,
            budget_used_ns=13_000,
            budget_window_start_ns=17_000,
        )
        new = flowctl.SessionPolicy(
            session_id=124,
            flags=flowctl.POLICY_ENABLE | flowctl.POLICY_FALLBACK,
            budget_used_ns=0,
            budget_window_start_ns=0,
        )

        with mock.patch.object(flowctl, "bpftool_lookup", return_value=old.pack_value()):
            merged = flowctl.merge_existing_policy(object(), new)

        self.assertTrue(merged.flags & flowctl.POLICY_FALLBACK)
        self.assertEqual(merged.budget_used_ns, old.budget_used_ns)
        self.assertEqual(merged.budget_window_start_ns, old.budget_window_start_ns)

    def test_policy_fallback_sets_policy_flag_without_dropping_frontier_hint(self) -> None:
        root = proc(
            400,
            1,
            "S",
            "make",
            "make -j2 all",
            children=(401,),
        )
        cc1 = proc(
            401,
            400,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[400, 401]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={400: root, 401: cc1}),
        ):
            plan = flowctl.build_hint_plan(
                400,
                "session:policy-fallback",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                True,
                flowctl.Stage.BUILD,
            )

        self.assertTrue(plan.policy.flags & flowctl.POLICY_FALLBACK)
        frontier_hints = [hint for hint in plan.hints if hint.flags & flowctl.TASK_FRONTIER]
        self.assertEqual([hint.pid for hint in frontier_hints], [401])


class AdaptiveWidthTests(unittest.TestCase):
    def test_adaptive_width_expands_for_wide_parent_wait_cohort(self) -> None:
        root = proc(
            500,
            1,
            "S",
            "python3",
            "python3 agent_replay.py",
            children=tuple(range(501, 509)),
        )
        infos = {500: root}
        for index, pid in enumerate(range(501, 509), start=1):
            infos[pid] = proc(
                pid,
                500,
                "R",
                "python3",
                "python3 test_worker.py",
                start_time=index,
                cpu_ticks=1,
            )

        with (
            mock.patch.object(flowctl, "subtree", return_value=sorted(infos)),
            mock.patch.object(flowctl, "collect_proc_info", return_value=infos),
        ):
            plan = flowctl.build_hint_plan(
                500,
                "session:adaptive-width",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                2,
                2,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.TEST,
                True,
                8,
            )

        frontier_hints = [hint for hint in plan.hints if hint.flags & flowctl.TASK_FRONTIER]
        self.assertEqual(plan.policy.max_frontier_width, 8)
        self.assertEqual(len(frontier_hints), 8)
        self.assertTrue(plan.adaptive_width["enabled"])
        self.assertEqual(plan.adaptive_width["serviceable_leaf_count"], 8)
        self.assertIn("wide-leaves=8", plan.adaptive_width["reason"])

    def test_static_width_keeps_requested_policy_width(self) -> None:
        root = proc(
            600,
            1,
            "S",
            "python3",
            "python3 agent_replay.py",
            children=tuple(range(601, 609)),
        )
        infos = {600: root}
        for index, pid in enumerate(range(601, 609), start=1):
            infos[pid] = proc(
                pid,
                600,
                "R",
                "python3",
                "python3 test_worker.py",
                start_time=index,
                cpu_ticks=1,
            )

        with (
            mock.patch.object(flowctl, "subtree", return_value=sorted(infos)),
            mock.patch.object(flowctl, "collect_proc_info", return_value=infos),
        ):
            plan = flowctl.build_hint_plan(
                600,
                "session:static-width",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                2,
                2,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.TEST,
            )

        frontier_hints = [hint for hint in plan.hints if hint.flags & flowctl.TASK_FRONTIER]
        self.assertEqual(plan.policy.max_frontier_width, 2)
        self.assertEqual(len(frontier_hints), 2)
        self.assertFalse(plan.adaptive_width["enabled"])


class FrontierActionTests(unittest.TestCase):
    def test_observe_action_sets_fallback_and_observe_flag(self) -> None:
        root = proc(
            650,
            1,
            "S",
            "make",
            "make -j2 all",
            children=(651,),
        )
        cc1 = proc(
            651,
            650,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[650, 651]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={650: root, 651: cc1}),
        ):
            plan = flowctl.build_hint_plan(
                650,
                "session:observe",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.BUILD,
                False,
                8,
                "observe",
            )

        frontier_hints = [hint for hint in plan.hints if hint.flags & flowctl.TASK_FRONTIER]
        self.assertTrue(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertEqual(plan.action["action"], "observe")
        self.assertEqual([hint.pid for hint in frontier_hints], [651])
        self.assertTrue(frontier_hints[0].flags & flowctl.TASK_OBSERVE_ONLY)

    def test_auto_action_observes_ambiguous_static_narrow_width(self) -> None:
        root = proc(
            660,
            1,
            "S",
            "python3",
            "python3 agent_replay.py",
            children=tuple(range(661, 669)),
        )
        infos = {660: root}
        for index, pid in enumerate(range(661, 669), start=1):
            infos[pid] = proc(
                pid,
                660,
                "R",
                "python3",
                "python3 test_worker.py",
                start_time=index,
                cpu_ticks=1,
            )

        with (
            mock.patch.object(flowctl, "subtree", return_value=sorted(infos)),
            mock.patch.object(flowctl, "collect_proc_info", return_value=infos),
        ):
            plan = flowctl.build_hint_plan(
                660,
                "session:auto-static",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                2,
                2,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.TEST,
                False,
                8,
                "auto",
            )

        frontier_hints = [hint for hint in plan.hints if hint.flags & flowctl.TASK_FRONTIER]
        self.assertEqual(len(frontier_hints), 2)
        self.assertEqual(plan.action["action"], "observe")
        self.assertIn("ambiguous-wide-cohort", plan.action["reason"])
        self.assertTrue(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertTrue(all(hint.flags & flowctl.TASK_OBSERVE_ONLY for hint in frontier_hints))

    def test_build_tail_only_admission_observes_single_compiler_tail(self) -> None:
        root = proc(
            670,
            1,
            "S",
            "make",
            "make -j2 all",
            children=(671,),
        )
        cc1 = proc(
            671,
            670,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[670, 671]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={670: root, 671: cc1}),
        ):
            plan = flowctl.build_hint_plan(
                670,
                "session:build-tail-only",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.BUILD,
                False,
                8,
                "advance",
                stage_aware_frontier_propagation_depth=True,
                build_frontier_admission="tail-only",
            )

        frontier_hint = next(hint for hint in plan.hints if hint.pid == 671)
        self.assertEqual(plan.frontier_pids, {671})
        self.assertEqual(plan.action["action"], "advance")
        self.assertTrue(plan.action["build_admission_triggered"])
        self.assertIn("build-tail-only-observe", plan.action["reason"])
        self.assertEqual(plan.action["task_observe_only_pids"], [671])
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertTrue(frontier_hint.flags & flowctl.TASK_OBSERVE_ONLY)

    def test_build_tail_only_admission_advances_link_tail(self) -> None:
        root = proc(
            680,
            1,
            "S",
            "make",
            "make -j2 all",
            children=(681,),
        )
        linker = proc(
            681,
            680,
            "R",
            "collect2",
            "/usr/lib/gcc/collect2 -o app",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[680, 681]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={680: root, 681: linker}),
        ):
            plan = flowctl.build_hint_plan(
                680,
                "session:build-link-tail",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.BUILD,
                False,
                8,
                "advance",
                stage_aware_frontier_propagation_depth=True,
                build_frontier_admission="tail-only",
            )

        frontier_hint = next(hint for hint in plan.hints if hint.pid == 681)
        self.assertEqual(plan.frontier_pids, {681})
        self.assertEqual(plan.action["action"], "advance")
        self.assertFalse(plan.action["build_admission_triggered"])
        self.assertIn("build-tail-only-advance", plan.action["reason"])
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertFalse(frontier_hint.flags & flowctl.TASK_OBSERVE_ONLY)


class PropagationAnchorTests(unittest.TestCase):
    def test_idle_sandbox_driver_can_be_propagation_anchor_without_being_frontier(self) -> None:
        runner = proc(
            680,
            1,
            "S",
            "python3",
            "python3 scripts/run_agent_sandbox_sched.py --workloads shell_mix --make-jobs 1",
            start_time=0,
            cpu_ticks=3,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[680]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={680: runner}),
        ):
            plan = flowctl.build_hint_plan(
                680,
                "session:spawn-anchor",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                2,
                2,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                None,
            )

        self.assertEqual(plan.frontier_pids, set())
        self.assertEqual(plan.propagation_pids, {680})
        runner_hint = next(hint for hint in plan.hints if hint.pid == 680)
        self.assertTrue(runner_hint.flags & flowctl.TASK_PROPAGATE)
        self.assertFalse(runner_hint.flags & flowctl.TASK_FRONTIER)
        self.assertEqual(runner_hint.propagation_depth, 2)

    def test_adaptive_propagation_depth_uses_one_for_concrete_frontier(self) -> None:
        root = proc(
            690,
            1,
            "S",
            "bash",
            "bash -lc make -j2",
            start_time=0,
            children=(691,),
        )
        make = proc(
            691,
            690,
            "S",
            "make",
            "make -j2 all",
            start_time=1,
            children=(692,),
        )
        cc1 = proc(
            692,
            691,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=2,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[690, 691, 692]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={690: root, 691: make, 692: cc1}),
        ):
            plan = flowctl.build_hint_plan(
                690,
                "session:adaptive-prop",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.BUILD,
                adaptive_frontier_propagation_depth=True,
            )

        self.assertEqual(plan.frontier_pids, {692})
        self.assertEqual(plan.propagation_pids, {691})
        anchor_hint = next(hint for hint in plan.hints if hint.pid == 691)
        self.assertEqual(anchor_hint.propagation_depth, 1)
        self.assertEqual(plan.adaptive_propagation["propagation_depth"], 1)
        self.assertEqual(plan.adaptive_propagation["reason"], "concrete-frontier-depth1")

    def test_adaptive_propagation_depth_uses_requested_depth_for_spawn_anchor(self) -> None:
        runner = proc(
            695,
            1,
            "S",
            "python3",
            "python3 scripts/run_agent_sandbox_sched.py --workloads shell_mix --make-jobs 1",
            start_time=0,
            cpu_ticks=3,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[695]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={695: runner}),
        ):
            plan = flowctl.build_hint_plan(
                695,
                "session:adaptive-spawn-anchor",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                2,
                2,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                None,
                adaptive_frontier_propagation_depth=True,
            )

        self.assertEqual(plan.frontier_pids, set())
        self.assertEqual(plan.propagation_pids, {695})
        runner_hint = next(hint for hint in plan.hints if hint.pid == 695)
        self.assertEqual(runner_hint.propagation_depth, 2)
        self.assertEqual(plan.adaptive_propagation["propagation_depth"], 2)
        self.assertEqual(plan.adaptive_propagation["reason"], "spawn-anchor-use-requested-depth")

    def test_stage_aware_propagation_depth_suppresses_broad_build_tail(self) -> None:
        root = proc(
            760,
            1,
            "S",
            "bash",
            "bash -lc make -j2",
            start_time=0,
            children=(761,),
        )
        make = proc(
            761,
            760,
            "S",
            "make",
            "make -j2 all",
            start_time=1,
            children=(762, 763),
        )
        cc1_a = proc(
            762,
            761,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/a.c",
            start_time=2,
            cpu_ticks=10,
        )
        cc1_b = proc(
            763,
            761,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/b.c",
            start_time=3,
            cpu_ticks=11,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[760, 761, 762, 763]),
            mock.patch.object(
                flowctl,
                "collect_proc_info",
                return_value={760: root, 761: make, 762: cc1_a, 763: cc1_b},
            ),
        ):
            plan = flowctl.build_hint_plan(
                760,
                "session:stage-aware-build",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.BUILD,
                stage_aware_frontier_propagation_depth=True,
            )

        self.assertEqual(plan.stage, flowctl.Stage.BUILD)
        self.assertEqual(plan.adaptive_propagation["policy"], "stage-aware")
        self.assertEqual(plan.adaptive_propagation["propagation_depth"], 0)
        self.assertEqual(
            plan.adaptive_propagation["reason"],
            "stage-aware-build-wide-or-weak-tail-depth0",
        )
        self.assertGreaterEqual(plan.adaptive_propagation["selected_max_barrier_tail"], 2)
        anchor_hint = next(hint for hint in plan.hints if hint.pid == 761)
        self.assertTrue(anchor_hint.flags & flowctl.TASK_PROPAGATE)
        self.assertEqual(anchor_hint.propagation_depth, 0)

    def test_stage_aware_propagation_depth_keeps_test_frontier_shallow(self) -> None:
        root = proc(
            770,
            1,
            "S",
            "sh",
            "sh -c python3 test_runner.py",
            start_time=0,
            children=(771,),
        )
        test_driver = proc(
            771,
            770,
            "S",
            "python3",
            "python3 test_runner.py",
            start_time=1,
            children=(772,),
        )
        test_worker = proc(
            772,
            771,
            "R",
            "test_runner",
            "test_runner --case unit",
            start_time=2,
            cpu_ticks=20,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[770, 771, 772]),
            mock.patch.object(
                flowctl,
                "collect_proc_info",
                return_value={770: root, 771: test_driver, 772: test_worker},
            ),
        ):
            plan = flowctl.build_hint_plan(
                770,
                "session:stage-aware-test",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.TEST,
                stage_aware_frontier_propagation_depth=True,
            )

        self.assertEqual(plan.stage, flowctl.Stage.TEST)
        self.assertEqual(plan.frontier_pids, {772})
        self.assertEqual(plan.adaptive_propagation["policy"], "stage-aware")
        self.assertEqual(plan.adaptive_propagation["propagation_depth"], 1)
        self.assertEqual(plan.adaptive_propagation["reason"], "stage-aware-test-barrier-depth1")
        anchor_hint = next(hint for hint in plan.hints if hint.pid == 771)
        self.assertEqual(anchor_hint.propagation_depth, 1)

    def test_stage_aware_propagation_depth_keeps_spawn_anchor_depth(self) -> None:
        runner = proc(
            780,
            1,
            "S",
            "python3",
            "python3 agent_workflow_replay.py --turns 3",
            start_time=0,
            cpu_ticks=3,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[780]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={780: runner}),
        ):
            plan = flowctl.build_hint_plan(
                780,
                "session:stage-aware-spawn",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                None,
                frontier_action="stage-aware",
                stage_aware_frontier_propagation_depth=True,
            )

        self.assertEqual(plan.frontier_pids, set())
        self.assertEqual(plan.propagation_pids, set())
        self.assertEqual(plan.adaptive_propagation["policy"], "stage-aware")
        self.assertEqual(plan.adaptive_propagation["propagation_depth"], 2)
        self.assertEqual(
            plan.adaptive_propagation["reason"],
            "stage-aware-spawn-anchor-use-requested-depth",
        )
        self.assertEqual(plan.action["action"], "advance")
        self.assertIn("stage-aware-no-frontier-suppress-propagation", plan.action["reason"])
        self.assertEqual(plan.action["task_suppress_propagation_pids"], [780])
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        runner_hint = next(hint for hint in plan.hints if hint.pid == 780)
        self.assertFalse(runner_hint.flags & flowctl.TASK_PROPAGATE)
        self.assertEqual(runner_hint.propagation_depth, 0)


class FrontierAutoAdmissionTests(unittest.TestCase):
    def test_normalize_frontier_action_accepts_stage_aware_aliases(self) -> None:
        self.assertEqual(flowctl.normalize_frontier_action("stage-aware"), "stage-aware")
        self.assertEqual(flowctl.normalize_frontier_action("stage"), "stage-aware")
        self.assertEqual(flowctl.normalize_frontier_action("stage_aware"), "stage-aware")

    def test_auto_observes_weak_single_python_test_worker(self) -> None:
        root = proc(
            790,
            1,
            "S",
            "python",
            "python harness.py",
            start_time=0,
            children=(791,),
        )
        worker = proc(
            791,
            790,
            "R",
            "python",
            "python manage.py test",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[790, 791]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={790: root, 791: worker}),
        ):
            plan = flowctl.build_hint_plan(
                790,
                "session:auto-weak-test-worker",
                75,
                -1,
                1000,
                100,
                10,
                500,
                False,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.TEST,
                frontier_action="auto",
            )

        hints = {hint.pid: hint for hint in plan.hints}
        self.assertEqual(plan.frontier_pids, {791})
        self.assertEqual(plan.action["action"], "observe")
        self.assertTrue(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertIn("weak-test-worker-frontier", plan.action["reason"])
        self.assertTrue(plan.action["test_selected_worker_only"])
        self.assertTrue(hints[791].flags & flowctl.TASK_FRONTIER)
        self.assertTrue(hints[791].flags & flowctl.TASK_OBSERVE_ONLY)

    def test_auto_advances_explicit_pytest_driver(self) -> None:
        root = proc(
            800,
            1,
            "S",
            "python",
            "python harness.py",
            start_time=0,
            children=(801,),
        )
        pytest = proc(
            801,
            800,
            "R",
            "python",
            "python -m pytest tests",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[800, 801]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={800: root, 801: pytest}),
        ):
            plan = flowctl.build_hint_plan(
                800,
                "session:auto-pytest-driver",
                75,
                -1,
                1000,
                100,
                10,
                500,
                False,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.TEST,
                frontier_action="auto",
            )

        hints = {hint.pid: hint for hint in plan.hints}
        self.assertEqual(plan.frontier_pids, {801})
        self.assertEqual(plan.action["action"], "advance")
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertTrue(plan.action["test_selected_driver"])
        self.assertTrue(hints[801].flags & flowctl.TASK_FRONTIER)
        self.assertFalse(hints[801].flags & flowctl.TASK_OBSERVE_ONLY)

    def test_stage_aware_suppresses_weak_single_python_test_worker(self) -> None:
        root = proc(
            810,
            1,
            "S",
            "python",
            "python harness.py",
            start_time=0,
            children=(811,),
        )
        worker = proc(
            811,
            810,
            "R",
            "python",
            "python manage.py test",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[810, 811]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={810: root, 811: worker}),
        ):
            plan = flowctl.build_hint_plan(
                810,
                "session:stage-aware-weak-test-worker",
                75,
                -1,
                1000,
                100,
                10,
                500,
                False,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.TEST,
                frontier_action="stage-aware",
            )

        hints = {hint.pid: hint for hint in plan.hints}
        self.assertEqual(plan.frontier_pids, set())
        self.assertEqual(plan.action["requested"], "stage-aware")
        self.assertEqual(plan.action["action"], "advance")
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertIn("stage-aware-test-worker-suppress", plan.action["reason"])
        self.assertEqual(plan.action["task_suppress_frontier_pids"], [811])
        self.assertTrue(plan.action["test_selected_worker_only"])
        self.assertFalse(hints[811].flags & flowctl.TASK_FRONTIER)
        self.assertFalse(hints[811].flags & flowctl.TASK_OBSERVE_ONLY)

    def test_stage_aware_advances_explicit_pytest_driver(self) -> None:
        root = proc(
            820,
            1,
            "S",
            "python",
            "python harness.py",
            start_time=0,
            children=(821,),
        )
        pytest = proc(
            821,
            820,
            "R",
            "python",
            "python -m pytest tests",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[820, 821]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={820: root, 821: pytest}),
        ):
            plan = flowctl.build_hint_plan(
                820,
                "session:stage-aware-pytest-driver",
                75,
                -1,
                1000,
                100,
                10,
                500,
                False,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.TEST,
                frontier_action="stage-aware",
            )

        hints = {hint.pid: hint for hint in plan.hints}
        self.assertEqual(plan.frontier_pids, {821})
        self.assertEqual(plan.action["action"], "advance")
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertIn("stage-aware-test-driver-advance", plan.action["reason"])
        self.assertTrue(plan.action["test_selected_driver"])
        self.assertTrue(hints[821].flags & flowctl.TASK_FRONTIER)
        self.assertFalse(hints[821].flags & flowctl.TASK_OBSERVE_ONLY)

    def test_stage_aware_build_suppresses_single_compiler_tail(self) -> None:
        root = proc(
            830,
            1,
            "S",
            "make",
            "make -j2 all",
            start_time=0,
            children=(831,),
        )
        cc1 = proc(
            831,
            830,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[830, 831]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={830: root, 831: cc1}),
        ):
            plan = flowctl.build_hint_plan(
                830,
                "session:stage-aware-build-compiler",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.BUILD,
                frontier_action="stage-aware",
                stage_aware_frontier_propagation_depth=True,
            )

        frontier_hint = next(hint for hint in plan.hints if hint.pid == 831)
        self.assertEqual(plan.frontier_pids, set())
        self.assertEqual(plan.action["action"], "advance")
        self.assertTrue(plan.action["build_admission_triggered"])
        self.assertIn("stage-aware-build-tail-suppress", plan.action["reason"])
        self.assertEqual(plan.action["task_suppress_frontier_pids"], [831])
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertFalse(frontier_hint.flags & flowctl.TASK_FRONTIER)
        self.assertFalse(frontier_hint.flags & flowctl.TASK_OBSERVE_ONLY)

    def test_stage_aware_build_advances_link_tail(self) -> None:
        root = proc(
            840,
            1,
            "S",
            "make",
            "make -j2 all",
            start_time=0,
            children=(841,),
        )
        linker = proc(
            841,
            840,
            "R",
            "collect2",
            "/usr/lib/gcc/collect2 -o app",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[840, 841]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={840: root, 841: linker}),
        ):
            plan = flowctl.build_hint_plan(
                840,
                "session:stage-aware-build-link",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.BUILD,
                frontier_action="stage-aware",
                stage_aware_frontier_propagation_depth=True,
            )

        frontier_hint = next(hint for hint in plan.hints if hint.pid == 841)
        self.assertEqual(plan.frontier_pids, {841})
        self.assertEqual(plan.action["action"], "advance")
        self.assertFalse(plan.action["build_admission_triggered"])
        self.assertIn("stage-aware-build-tail-advance", plan.action["reason"])
        self.assertFalse(plan.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertFalse(frontier_hint.flags & flowctl.TASK_OBSERVE_ONLY)


class FrontierStabilityTests(unittest.TestCase):
    def build_single_frontier_plan(self) -> flowctl.HintPlan:
        make = proc(
            700,
            1,
            "S",
            "make",
            "make -j2 all",
            start_time=0,
            children=(701,),
        )
        cc1 = proc(
            701,
            700,
            "R",
            "cc1",
            "/usr/lib/gcc/x86_64-linux-gnu/cc1 src/unit.c",
            start_time=1,
            cpu_ticks=10,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[700, 701]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={700: make, 701: cc1}),
        ):
            return flowctl.build_hint_plan(
                700,
                "session:stability",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                False,
                0,
                False,
                flowctl.Stage.BUILD,
            )

    def test_stability_gate_delays_weak_new_frontier_until_second_sample(self) -> None:
        plan = self.build_single_frontier_plan()
        state = flowctl.FrontierStabilityState()

        first = flowctl.apply_frontier_stability(
            plan,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=2,
            hold_ms=0,
            bypass_score=0,
        )
        second = flowctl.apply_frontier_stability(
            plan,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=2,
            hold_ms=0,
            bypass_score=0,
        )

        self.assertEqual(first.frontier_pids, set())
        self.assertEqual(first.stability["suppressed_count"], 1)
        self.assertEqual(second.frontier_pids, {701})
        self.assertEqual(second.stability["suppressed_count"], 0)

    def test_observe_until_stable_keeps_frontier_hint_but_falls_back(self) -> None:
        plan = self.build_single_frontier_plan()
        state = flowctl.FrontierStabilityState()

        first = flowctl.apply_frontier_stability(
            plan,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=2,
            hold_ms=0,
            bypass_score=0,
            stability_action="observe",
        )
        second = flowctl.apply_frontier_stability(
            plan,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=2,
            hold_ms=0,
            bypass_score=0,
            stability_action="observe",
        )

        first_frontier = next(hint for hint in first.hints if hint.pid == 701)
        second_frontier = next(hint for hint in second.hints if hint.pid == 701)
        self.assertEqual(first.frontier_pids, {701})
        self.assertTrue(first.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertTrue(first_frontier.flags & flowctl.TASK_FRONTIER)
        self.assertTrue(first_frontier.flags & flowctl.TASK_OBSERVE_ONLY)
        self.assertEqual(first.action["action"], "observe")
        self.assertIn("stability-observe-until-stable", first.action["reason"])
        self.assertEqual(first.stability["stable_selected"], [])
        self.assertEqual(first.stability["observed_unstable"], [701])

        self.assertEqual(second.frontier_pids, {701})
        self.assertFalse(second.policy.flags & flowctl.POLICY_FALLBACK)
        self.assertTrue(second_frontier.flags & flowctl.TASK_FRONTIER)
        self.assertFalse(second_frontier.flags & flowctl.TASK_OBSERVE_ONLY)
        self.assertEqual(second.action["action"], "advance")
        self.assertEqual(second.stability["stable_selected"], [701])
        self.assertEqual(second.stability["observed_unstable"], [])

    def test_stability_gate_allows_strong_evidence_to_bypass_debounce(self) -> None:
        plan = self.build_single_frontier_plan()
        state = flowctl.FrontierStabilityState()
        top_score = plan.ranked_frontiers[0][0]

        admitted = flowctl.apply_frontier_stability(
            plan,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=3,
            hold_ms=0,
            bypass_score=top_score,
        )

        self.assertEqual(admitted.frontier_pids, {701})
        self.assertEqual(admitted.stability["suppressed_count"], 0)

    def test_selected_stability_requires_same_selected_frontier_to_persist(self) -> None:
        policy = flowctl.SessionPolicy(session_id=11, flags=flowctl.POLICY_ENABLE | flowctl.POLICY_WIDTH_CTL)
        base_hints = [
            flowctl.TaskHint(pid=700, tgid=700, parent_pid=1, session_id=11, stage_id=1, cohort_id=1, flags=0, confidence=0),
            flowctl.TaskHint(pid=701, tgid=701, parent_pid=700, session_id=11, stage_id=1, cohort_id=1, flags=flowctl.TASK_FRONTIER, confidence=75),
            flowctl.TaskHint(pid=702, tgid=702, parent_pid=700, session_id=11, stage_id=1, cohort_id=1, flags=0, confidence=0),
        ]
        first_top = flowctl.HintPlan(
            root_pid=700,
            policy=policy,
            hints=base_hints,
            stage=flowctl.Stage.TEST,
            frontier_pids={701},
            propagation_pids=set(),
            ranked_frontiers=[
                (300, 701, "test worker,state=R,leaf,parent-wait:python3"),
                (299, 702, "test worker,state=R,leaf,parent-wait:python3"),
            ],
            proc_comms={700: "python3", 701: "test_runner", 702: "test_runner"},
        )
        second_top = flowctl.HintPlan(
            root_pid=700,
            policy=policy,
            hints=[
                base_hints[0],
                flowctl.TaskHint(pid=701, tgid=701, parent_pid=700, session_id=11, stage_id=1, cohort_id=1, flags=0, confidence=0),
                flowctl.TaskHint(pid=702, tgid=702, parent_pid=700, session_id=11, stage_id=1, cohort_id=1, flags=flowctl.TASK_FRONTIER, confidence=75),
            ],
            stage=flowctl.Stage.TEST,
            frontier_pids={702},
            propagation_pids=set(),
            ranked_frontiers=[
                (301, 702, "test worker,state=R,leaf,parent-wait:python3"),
                (300, 701, "test worker,state=R,leaf,parent-wait:python3"),
            ],
            proc_comms={700: "python3", 701: "test_runner", 702: "test_runner"},
        )
        state = flowctl.FrontierStabilityState()

        first = flowctl.apply_frontier_stability(
            first_top,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=2,
            hold_ms=0,
            bypass_score=0,
            selected_only=True,
        )
        churned = flowctl.apply_frontier_stability(
            second_top,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=2,
            hold_ms=0,
            bypass_score=0,
            selected_only=True,
        )
        stable = flowctl.apply_frontier_stability(
            second_top,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=2,
            hold_ms=0,
            bypass_score=0,
            selected_only=True,
        )

        self.assertEqual(first.frontier_pids, set())
        self.assertEqual(churned.frontier_pids, set())
        self.assertEqual(stable.frontier_pids, {702})
        self.assertEqual(stable.stability["mode"], "selected")

    def test_stability_preserves_spawn_anchor_when_no_concrete_frontier(self) -> None:
        root = proc(
            780,
            1,
            "S",
            "python3",
            "python3 agent_workflow_replay.py",
            start_time=0,
            cpu_ticks=1,
        )

        with (
            mock.patch.object(flowctl, "subtree", return_value=[780]),
            mock.patch.object(flowctl, "collect_proc_info", return_value={780: root}),
        ):
            plan = flowctl.build_hint_plan(
                780,
                "session:spawn-stability",
                75,
                -1,
                1000,
                100,
                10,
                500,
                True,
                1,
                1,
                flowctl.MIN_FRONTIER_SCORE,
                True,
                2,
                False,
                flowctl.Stage.BUILD,
                stage_aware_frontier_propagation_depth=True,
            )

        state = flowctl.FrontierStabilityState()
        stable = flowctl.apply_frontier_stability(
            plan,
            state,
            frontier_width=1,
            min_frontier_score=flowctl.MIN_FRONTIER_SCORE,
            stability_samples=3,
            hold_ms=0,
            bypass_score=0,
            selected_only=True,
        )

        hints = {hint.pid: hint for hint in stable.hints}
        self.assertEqual(stable.frontier_pids, set())
        self.assertEqual(stable.propagation_pids, {780})
        self.assertTrue(hints[780].flags & flowctl.TASK_PROPAGATE)
        self.assertEqual(hints[780].propagation_depth, 2)


if __name__ == "__main__":
    unittest.main()
