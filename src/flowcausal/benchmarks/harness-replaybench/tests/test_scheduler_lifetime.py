"""A completed workload must not hide a scheduler that exited early."""
import importlib.util
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

PATH = Path(__file__).resolve().parents[1]/'adapters/run_scheduler.py'
SPEC = importlib.util.spec_from_file_location('scheduler_lifetime_adapter', PATH)
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class SchedulerLifetimeTests(unittest.TestCase):
    def test_linux_label_rejects_a_scheduler_or_controller_before_start(self):
        for option in ['--binary', '--tool-controller']:
            with self.subTest(option=option):
                argv=['adapter','--scheduler','linux','--manifest','unused',
                      '--output','unused',option,'/does-not-exist']
                error=io.StringIO()
                with mock.patch.object(sys,'argv',argv), \
                     mock.patch.object(M,'scx_state') as state, \
                     mock.patch.object(M.subprocess,'Popen') as start, \
                     contextlib.redirect_stderr(error):
                    with self.assertRaises(SystemExit) as raised:
                        M.main()
                    self.assertEqual(raised.exception.code,2)
                    state.assert_not_called()
                    start.assert_not_called()
                self.assertIn('原生 Linux',error.getvalue())

    def test_linux_label_cannot_hide_an_active_scx_scheduler(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); stats=root/'stats'; stats.write_text('0')
            argv=['adapter','--scheduler','linux','--manifest',str(root/'manifest'),
                  '--output',str(root/'output')]
            with mock.patch.object(sys,'argv',argv), mock.patch.object(M,'BPF_STATS',stats), \
                 mock.patch.object(M,'scx_state',return_value=('enabled','another_scheduler')), \
                 mock.patch.object(M.subprocess,'run') as run:
                with self.assertRaisesRegex(RuntimeError,'已有 sched_ext'):
                    M.main()
                run.assert_not_called()

    def test_early_scheduler_exit_invalidates_successful_workload(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); stats=root/'stats'; stats.write_text('0')
            binary=root/'scheduler'; binary.write_text('placeholder'); binary.chmod(0o755)
            output=root/'output'
            argv=['adapter','--scheduler','test','--binary',str(binary),
                  '--manifest',str(root/'manifest'),'--output',str(output)]
            process=mock.Mock(pid=12345)
            process.poll.return_value=1
            process.wait.return_value=1
            def complete_work(*args,**kwargs):
                output.mkdir()
                (output/'result.json').write_text(json.dumps({'status':'pass'}))
                return mock.Mock(returncode=0)
            with mock.patch.object(sys,'argv',argv), mock.patch.object(M,'BPF_STATS',stats), \
                 mock.patch.object(M,'scx_state',return_value=('disabled','')), \
                 mock.patch.object(M,'wait_state',side_effect=[('enabled','test'),('disabled','')]), \
                 mock.patch.object(M.subprocess,'Popen',return_value=process), \
                 mock.patch.object(M.subprocess,'run',side_effect=complete_work), \
                 mock.patch.object(M.os,'killpg'):
                self.assertEqual(M.main(),1)
            result=json.loads((output/'result.json').read_text())
            self.assertEqual(result['status'],'fail')
            self.assertFalse(result['tested_system']['scheduler_valid'])
            self.assertFalse(result['tested_system']['scheduler_alive_at_workload_end'])


if __name__=='__main__':unittest.main()
