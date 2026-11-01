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
        config = ModuleType('hermes_cli.config')
        config.load_config_readonly = Mock(return_value={'skills': {'auto_load': ['expert-review']}})
        skill_commands = ModuleType('agent.skill_commands')
        skill = (Path(__file__).resolve().parents[1] / 'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        body = skill.split('---', 2)[2].strip()
        skill_commands.build_auto_load_prompt = Mock(return_value=(body, ['expert-review'], []))
        package = ModuleType('hermes_cli')
        modules = {'toolsets': toolsets, 'hermes_cli': package, 'hermes_cli.main': cli,
                   'hermes_cli.config': config, 'agent.skill_commands': skill_commands}
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

    def test_unconfigured_or_extra_preloaded_skill_blocks_gateway(self):
        for value in (None, [], ['other'], ['expert-review', 'other']):
            with self.subTest(auto_load=value):
                runner, toolsets, cli, modules = self.runner()
                modules['hermes_cli.config'].load_config_readonly.return_value = {'skills': {'auto_load': value}}
                with patch.dict(sys.modules, modules), self.assertRaisesRegex(RuntimeError, 'preload exactly'):
                    runner.main()
                cli.main.assert_not_called()

    def test_missing_disabled_or_partial_preload_blocks_gateway(self):
        for result in (('', [], ['expert-review']), ('partial skill', ['expert-review'], []),
                       ('unexpected skill', ['other'], [])):
            with self.subTest(result=result):
                runner, toolsets, cli, modules = self.runner()
                modules['agent.skill_commands'].build_auto_load_prompt.return_value = result
                with patch.dict(sys.modules, modules), self.assertRaisesRegex(RuntimeError, 'incomplete or disabled'):
                    runner.main()
                cli.main.assert_not_called()
