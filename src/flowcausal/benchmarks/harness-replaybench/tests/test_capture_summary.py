"""Session events remain observable when optional per-tool accounting is off."""
import importlib.util
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'runners'))
SPEC = importlib.util.spec_from_file_location('capture_summary_runner', ROOT / 'runners/run_suite.py')
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)


class CaptureSummaryTests(unittest.TestCase):
    def test_event_counts_do_not_depend_on_optional_tool_accounting(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            output = root / 'capture.jsonl'
            rows = [
                dict(record='session_summary', exits=7, wait_returns=5),
                dict(record='session_summary', exits=3, wait_returns=2),
                dict(record='tool_summary', exits=0, wait_returns=0, tool_returns=1),
                dict(record='capture_end', elapsed_ns=100),
            ]
            output.write_text(''.join(json.dumps(row) + '\n' for row in rows))
            process = mock.Mock()
            process.wait.return_value = 0
            capture = M.KernelCapture(process, output, io.StringIO(), root, root / 'socket', 'example')
            with mock.patch.object(M, 'send_mark'):
                result = M.finish_kernel_capture(capture, 1)
            self.assertEqual(result['process_exits'], 10)
            self.assertEqual(result['wait_returns'], 7)
            self.assertEqual(result['tool_returns'], 1)
            self.assertTrue(result['capture_end_recorded'])


if __name__ == '__main__':
    unittest.main()
