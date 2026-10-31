import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tools'/f'{name}.py')
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SourceEnvironmentRepairTests(unittest.TestCase):
    def test_cfdm_stabilizer_enters_actual_nested_repository(self):
        builder=load('build_trace_v2')
        command=dict(id='trajectory[14].tool_calls[0]',cmd='original command')
        builder.stabilize_recorded_artifacts('NCAS-CMS__cfdm-114',command)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            repo=root/'nested repository'
            target=repo/'cfdm/test'
            target.mkdir(parents=True)
            change_directory=command['replay_cmd'].split('&&',1)[0]
            result=subprocess.run(['bash','-c',change_directory+'&& pwd'],cwd=root,
                                  env=dict(os.environ,HRB_REPLAY_REPO=str(repo)),
                                  capture_output=True,text=True,check=True)
            self.assertEqual(result.stdout.strip(),str(target))
        self.assertEqual(command['cmd'],'original command')

    def test_replay_image_override_keeps_recorded_image(self):
        builder=load('build_v1_suites')
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            path=root/'trace.json'
            trace=dict(instance_id='task',source_task=dict(container_image='original'),
                       workspace=dict(container_repo_dir='/testbed'))
            path.write_text(json.dumps(trace))
            with patch.object(builder,'ROOT',root):
                self.assertEqual(builder.case_from_trace(path)['container_image'],'original')
                (root/'environments').mkdir()
                (root/'environments/replay-images.json').write_text(json.dumps(
                    {'task':{'container_image':'repaired'}}))
                self.assertEqual(builder.case_from_trace(path)['container_image'],'repaired')
            self.assertEqual(json.loads(path.read_text())['source_task']['container_image'],'original')


if __name__=='__main__':
    unittest.main()
