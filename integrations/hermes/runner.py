"""项目 Hermes 启动扩展：注册仅读技能工具集，不修改框架源码。"""
from toolsets import create_custom_toolset
from pathlib import Path


def verify_project_context(root):
    from hermes_cli.config import get_hermes_home
    from tools.terminal_scope import build_profile_terminal_scope
    from agent.prompt_builder import build_context_files_prompt

    scope = build_profile_terminal_scope(get_hermes_home())
    cwd = scope.get('TERMINAL_CWD')
    if not cwd or not Path(cwd).is_absolute() or Path(cwd).resolve() != root.resolve():
        raise RuntimeError('Hermes profile working directory must match the project mount')
    body = (root / 'AGENTS.md').read_text(encoding='utf-8').strip()
    prompt = build_context_files_prompt(cwd=cwd, skip_soul=True, allow_install_tree_fallback=False)
    if not body or body not in prompt:
        raise RuntimeError('Hermes profile project context is missing or incomplete')


def verify_preloaded_skill(root):
    from hermes_cli.config import load_config_readonly
    from agent.skill_commands import build_auto_load_prompt

    config = load_config_readonly()
    if config.get('skills', {}).get('auto_load') != ['expert-review']:
        raise RuntimeError('Hermes must preload exactly the project expert-review skill')
    prompt, loaded, missing = build_auto_load_prompt(user_config=config)
    skill = (root / 'skills/expert-review/SKILL.md').read_text(encoding='utf-8')
    body = skill.split('---', 2)[2].strip() if skill.startswith('---\n') else skill.strip()
    if loaded != ['expert-review'] or missing or not body or body not in prompt:
        raise RuntimeError('Hermes project skill preload is incomplete or disabled')


def main():
    root = Path(__file__).resolve().parent
    if not (root / 'AGENTS.md').is_file() or not (root / 'skills/expert-review/SKILL.md').is_file():
        raise RuntimeError('Hermes project context and expert-review skill must be mounted')
    verify_project_context(root)
    create_custom_toolset(
        'expert-skills', '只读专家审核技能', tools=['skills_list', 'skill_view'],
    )
    verify_preloaded_skill(root)
    from hermes_cli.main import main as hermes_main
    hermes_main()


if __name__ == '__main__':
    main()
