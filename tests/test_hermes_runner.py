import importlib.util
from pathlib import Path
import sys
from types import ModuleType
import unittest
from unittest.mock import Mock, patch


class HermesRunnerTests(unittest.TestCase):
    def runner(self):
        toolsets = ModuleType('toolsets')
        toolsets.create_custom_toolset = Mock()
        cli = ModuleType('hermes_cli.main')
        cli.main = Mock()
        package = ModuleType('hermes_cli')
        modules = {'toolsets': toolsets, 'hermes_cli': package, 'hermes_cli.main': cli}
        spec = importlib.util.spec_from_file_location('project_hermes_runner', Path(__file__).resolve().parents[1] / 'integrations/hermes/runner.py')
        runner = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, modules):
            spec.loader.exec_module(runner)
        return runner, toolsets, cli, modules

    def test_mounted_skill_starts_gateway_with_only_read_skills_toolset(self):
        runner, toolsets, cli, modules = self.runner()
        with patch.dict(sys.modules, modules):
            runner.main()
        self.assertEqual(['skills_list', 'skill_view'], toolsets.create_custom_toolset.call_args.kwargs['tools'])
        cli.main.assert_called_once_with()

    def test_missing_project_mount_fails_before_gateway_starts(self):
        runner, toolsets, cli, modules = self.runner()
        with patch.dict(sys.modules, modules), patch.object(Path, 'is_file', return_value=False):
            with self.assertRaisesRegex(RuntimeError, 'must be mounted'):
                runner.main()
        toolsets.create_custom_toolset.assert_not_called()
        cli.main.assert_not_called()
