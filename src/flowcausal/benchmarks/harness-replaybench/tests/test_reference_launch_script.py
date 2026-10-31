"""Changing interpreter-launch behavior invalidates cached measurements."""
import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('reference_launch_identity', ROOT/'tools/run_reference_baselines.py')
M = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = M
SPEC.loader.exec_module(M)


class LaunchScriptIdentityTest(unittest.TestCase):
    def test_shell_launcher_change_invalidates_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/'runners').mkdir()
            adapter = root/'adapter.py'
            adapter.write_text('pass\n')
            launch = root/'runners/launch_replay.sh'
            launch.write_text('exec python3 "$@"\n')
            with mock.patch.object(M, 'ROOT', root), mock.patch.object(M, 'ADAPTER', adapter):
                def identity():
                    return M.method_input_identity('manifest', M.Method('linux',''), [], False, None, [], True)['digest']
                before = identity()
                launch.write_text('exec /opt/python3 "$@"\n')
                self.assertNotEqual(before, identity())


if __name__ == '__main__':
    unittest.main()
