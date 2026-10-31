import importlib.util
import json
import pathlib
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "tools" / "analyze_return_recognition.py"
SPEC = importlib.util.spec_from_file_location("return_recognition", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def write_lines(path: pathlib.Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


class ReturnRecognitionTest(unittest.TestCase):
    def test_registered_root_and_wait_target_are_counted_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            trial = pathlib.Path(temporary)
            case = trial / "cases" / "00-example"
            case.mkdir(parents=True)
            (trial / "result.json").write_text(
                json.dumps({
                    "flowcausal_control": {
                        "sessions": [{
                            "instance_id": "example",
                            "stats": {"session_id": 7},
                        }]
                    }
                }),
                encoding="utf-8",
            )
            write_lines(case / "events.jsonl", [
                {"record": "command_begin", "instance_id": "example", "command_index": 0, "time_ns": 100},
                {"record": "command_end", "instance_id": "example", "command_index": 0, "time_ns": 200},
            ])
            write_lines(case / "flowd.log", [
                {"time_ns": 130, "kind": MODULE.WAIT_LINK, "session_id": 7, "sequence": 1, "pid": 1, "related_pid": 2, "reason": 3, "task_flags": MODULE.WAIT_TARGET},
                {"time_ns": 140, "kind": MODULE.ROLE_CHANGE, "session_id": 7, "sequence": 1, "pid": 1, "related_pid": 0, "reason": MODULE.RETURN_REASON, "task_flags": MODULE.RETURN_ROOT},
                {"time_ns": 150, "kind": MODULE.ROLE_CHANGE, "session_id": 7, "sequence": 1, "pid": 2, "related_pid": 0, "reason": MODULE.RETURN_REASON, "task_flags": MODULE.WAIT_TARGET},
            ])
            write_lines(trial / "kernel-capture.jsonl", [
                {"record": "task_summary", "session_id": 7, "pid": 1, "parent_pid": 0, "birth_ns": 90, "exit_ns": 190},
                {"record": "task_summary", "session_id": 7, "pid": 2, "parent_pid": 1, "birth_ns": 110, "exit_ns": 180},
            ])

            report = MODULE.analyze(trial)

            self.assertEqual(report["promotions"], 2)
            self.assertEqual(report["unique_promoted_tasks"], 2)
            self.assertEqual(report["fully_correlated"], 2)
            self.assertEqual(report["sessions"][0]["registered_return_roots"], 1)
            self.assertEqual(report["sessions"][0]["observed_wait_targets"], 1)
            self.assertEqual(report["sessions"][0]["inferred_tail_members"], 0)


if __name__ == "__main__":
    unittest.main()
