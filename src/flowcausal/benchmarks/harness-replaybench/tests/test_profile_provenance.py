import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]/'tools'


def module(name):
    spec = importlib.util.spec_from_file_location(name, TOOLS/f'{name}.py')
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


behavior = module('analyze_behavior_profiles')
opportunities = module('analyze_scheduling_opportunities')


class ProfileProvenanceTests(unittest.TestCase):
    def test_mixed_run_is_not_single_task_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)/'mixed'
            path.mkdir()
            (path/'result.json').write_text(json.dumps(dict(cases=[dict(instance_id='a'),dict(instance_id='b')])))
            with self.assertRaises(ValueError):
                behavior.result_directories(Path(temporary))

    def test_timeout_excluded_from_normal_return_relations(self):
        window = dict(command_id='a',command_index=0,stage='TEST',selected_tool_milestone=True,timed_out=True)
        profile = dict(instance_id='a',trace_file='trace',kernel=dict(raw=dict(
            tool_windows=[window],join_windows=[window],cross_generation_windows=[window])))
        relation = behavior.execution_relations(profile,'test')
        self.assertEqual(relation['join_windows'],[])
        self.assertEqual(relation['causal_windows'],[])
        self.assertEqual(relation['selected_tool_milestones'],[])

    def test_timeout_is_not_a_normal_completion_opportunity(self):
        window = dict(elapsed_sec=60,synchronous=True,older_generation_live_tasks=4,
                      older_generation_overlap_task_sec=12,older_generation_runtime_in_window_lower_bound_sec=6,
                      owned_runnable_wait_in_window_lower_bound_sec=6,owned_runnable_wait_sec=6,
                      owned_runnable_wait_fraction=.1,owned_peak_live_tasks=4)
        self.assertTrue(opportunities.confirmed_cross_tool_contention(window))
        self.assertEqual(opportunities.opportunity_level(window),'强候选')
        window['timed_out']=True
        self.assertFalse(opportunities.confirmed_cross_tool_contention(window))
        self.assertNotEqual(opportunities.opportunity_level(window),'强候选')

    def test_release_time_and_provenance_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            (root/'trace.json').write_text('{"commands": []}')
            output=root/'a'
            output.mkdir()
            data=dict(status='pass',cases=[dict(instance_id='a',elapsed_sec=1,workflow_completion_sec=1.5,
                                               stage_seconds={})],
                      experiment=dict(timing_semantics='release-to-completion-v1'))
            (output/'result.json').write_text(json.dumps(data))
            with patch.object(behavior,'ROOT',root):
                profile=behavior.build_profile(dict(instance_id='a',trace_file='trace.json'),root,{'a':output})
            self.assertEqual(profile['measurement_provenance']['result_file'], 'a/result.json')
            self.assertEqual(profile['replay_elapsed_sec'],1)
            self.assertEqual(profile['workflow_completion_sec'],1.5)
            self.assertEqual(profile['measurement_provenance']['result_sha256'],behavior.file_sha256(output/'result.json'))
            self.assertIsNone(profile['kernel']['complete'])


if __name__=='__main__':
    unittest.main()
