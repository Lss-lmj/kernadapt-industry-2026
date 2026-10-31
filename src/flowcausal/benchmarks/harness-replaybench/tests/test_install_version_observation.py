import importlib.util
from pathlib import Path
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('install_work_observation',ROOT/'runners/work_observation.py')
M=importlib.util.module_from_spec(spec);spec.loader.exec_module(M)
class InstallVersionTests(unittest.TestCase):
 def test_version_change_is_detected_despite_same_exit_and_tests(self):
  with tempfile.TemporaryDirectory() as directory:
   p=Path(directory)/'stdout';t={'instance_id':'case','commands':[{'cmd':'pip install multidict; pytest','id':'c'}]}
   def observe(version):
    p.write_text('Successfully installed multidict-'+version+'\n1 passed in 0.1s\n')
    return M.observe([{'record':'command_end','command_index':0,'command_id':'c','returncode':0,'output_span':[0,p.stat().st_size]}],p,t)
   result=M.compare(observe('6.9.0'),observe('6.9.1'))
   self.assertFalse(result['match'])
   self.assertEqual([d['field'] for d in result['differences']],['installed_distributions'])
 def test_printed_package_order_does_not_create_a_difference(self):
  self.assertEqual(M.summarize_output('Successfully installed a-1.0 b-2.0'),M.summarize_output('Successfully installed b-2.0 a-1.0'))
if __name__=='__main__':unittest.main()
