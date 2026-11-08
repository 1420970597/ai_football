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
        config.get_hermes_home = Mock(return_value=Path('/isolated-hermes-home'))
        config.load_config_readonly = Mock(return_value={'skills': {'auto_load': ['expert-review']}})
        root = Path(__file__).resolve().parents[1] / 'integrations/hermes'
        terminal_scope = ModuleType('tools.terminal_scope')
        terminal_scope.build_profile_terminal_scope = Mock(return_value={'TERMINAL_CWD': str(root)})
        prompt_builder = ModuleType('agent.prompt_builder')
        prompt_builder.build_context_files_prompt = Mock(return_value=(root / 'AGENTS.md').read_text())
        skill_commands = ModuleType('agent.skill_commands')
        skill = (Path(__file__).resolve().parents[1] / 'integrations/hermes/skills/expert-review/SKILL.md').read_text()
        body = skill.split('---', 2)[2].strip()
        skill_commands.build_auto_load_prompt = Mock(return_value=(body, ['expert-review'], []))
        package = ModuleType('hermes_cli')
        modules = {'toolsets': toolsets, 'hermes_cli': package, 'hermes_cli.main': cli,
                   'hermes_cli.config': config, 'agent.skill_commands': skill_commands,
                   'tools.terminal_scope': terminal_scope, 'agent.prompt_builder': prompt_builder}
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

    def test_preloaded_context_uses_actual_sport_instead_of_tool_server_name(self):
        runner, _, _, modules = self.runner()
        root = Path(runner.__file__).parent
        with patch.dict(sys.modules, modules):
            runner.verify_project_context(root)
            runner.verify_preloaded_skill(root)
        context = modules['agent.prompt_builder'].build_context_files_prompt.return_value
        skill = modules['agent.skill_commands'].build_auto_load_prompt.return_value[0]
        for text in (context, skill):
            self.assertIn('sport=basketball', text)
            self.assertIn('sport=football', text)
            self.assertIn('省略sport过滤', text)
        self.assertNotIn('每次search_matches显式传sport=football', skill)
        self.assertIn('迈阿密热火+6.5分', skill)
        self.assertIn('selection_line=+6.5', skill)
        self.assertIn('sport与范围在同一查询的各游标页保持一致', skill)
        self.assertIn('football与football_parallel是工具服务器名称', context)
        for text in ('逐场区分赛果反馈与新增推荐', '本身不代表旧单', '只有本场有回顾证据',
                     '正文无需再次写方向或盘口', '缺有效报价不等于没有推荐意图'):
            self.assertIn(text, skill)

    def test_gateway_profile_cwd_cannot_silently_resolve_to_home(self):
        for cwd in (None, '', '.', '/root', '/workspace-missing'):
            with self.subTest(cwd=cwd):
                runner, toolsets, cli, modules = self.runner()
                modules['tools.terminal_scope'].build_profile_terminal_scope.return_value = {'TERMINAL_CWD': cwd}
                with patch.dict(sys.modules, modules), self.assertRaisesRegex(RuntimeError, 'working directory'):
                    runner.main()
                cli.main.assert_not_called()
                toolsets.create_custom_toolset.assert_not_called()

    def test_missing_or_truncated_context_cannot_start_gateway(self):
        for prompt in ('', '# Project Context\npartial rules'):
            with self.subTest(prompt=prompt):
                runner, toolsets, cli, modules = self.runner()
                modules['agent.prompt_builder'].build_context_files_prompt.return_value = prompt
                with patch.dict(sys.modules, modules), self.assertRaisesRegex(RuntimeError, 'missing or incomplete'):
                    runner.main()
                cli.main.assert_not_called()
                toolsets.create_custom_toolset.assert_not_called()

    def test_context_verification_uses_profile_scope_and_disables_install_fallback(self):
        runner, _, _, modules = self.runner()
        root = Path(runner.__file__).parent
        with patch.dict(sys.modules, modules):
            runner.verify_project_context(root)
        modules['tools.terminal_scope'].build_profile_terminal_scope.assert_called_once_with(Path('/isolated-hermes-home'))
        modules['agent.prompt_builder'].build_context_files_prompt.assert_called_once_with(
            cwd=str(root), skip_soul=True, allow_install_tree_fallback=False)

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
