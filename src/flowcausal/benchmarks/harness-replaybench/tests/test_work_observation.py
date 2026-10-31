import importlib.util
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('work_observation', ROOT/'runners/work_observation.py')
M = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(M)


class WorkObservationTests(unittest.TestCase):
    def test_timing_and_warning_counts_do_not_change_completed_tests(self):
        a = M.summarize_output('=== 12 passed, 2 skipped, 7 warnings in 1.30s ===\n')
        b = M.summarize_output('=== 12 passed, 2 skipped in 8.01s ===\n')
        self.assertEqual(a, b)
        self.assertNotEqual(a, M.summarize_output('=== 11 passed, 2 skipped in 1.30s ===\n'))

    def test_unittest_and_collection_failures_are_retained(self):
        a = M.summarize_output('Ran 17 tests in 1.22s\nFAILED (errors=2)\nModuleNotFoundError: foo\n')
        self.assertEqual(a['test_outcomes'][0]['tests'], 17)
        self.assertEqual(a['exception_types'], ['ModuleNotFoundError'])
        self.assertTrue(M.summarize_output('2 errors in 0.31s')['test_outcomes'])

    def test_observation_preserves_unknown_source_and_detects_different_work(self):
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d)/'output';p.write_bytes(b'old output\n3 passed in 0.20s\n')
            trace = {'instance_id':'example','commands':[{'cmd':'pytest','id':'a'}]}
            events = [{'record':'command_end','command_index':0,'command_id':'a',
                       'returncode':0,'output_span':[11,p.stat().st_size]}]
            a=M.observe(events,p,trace)
            self.assertFalse(a['commands'][0]['source_returncode_known'])
            self.assertTrue(M.compare(a,a)['match'])
            p.write_bytes(b'old output\n2 passed in 0.20s\n')
            b=M.observe(events,p,trace)
            self.assertFalse(M.compare(a,b)['match'])
            b=M.observe([],p,trace)
            self.assertFalse(M.compare(a,b)['complete'])

    def test_async_output_is_not_attributed_to_a_single_command(self):
        with tempfile.TemporaryDirectory() as d:
            p=pathlib.Path(d)/'output';p.write_text('1 passed in 0.1s\n')
            t={'instance_id':'example','commands':[{'cmd':'pytest','id':'a','async':True}]}
            a=M.observe([{'record':'command_end','command_index':0,'command_id':'a',
                         'returncode':0,'output_span':[0,p.stat().st_size]}],p,t)
            self.assertEqual(a['commands'][0]['output_attribution'],'shared_async')


if __name__=='__main__':unittest.main()
