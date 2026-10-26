"""项目 Hermes 启动扩展：注册仅读技能工具集，不修改框架源码。"""
from toolsets import create_custom_toolset


def main():
    create_custom_toolset(
        'expert-skills', '只读专家审核技能', tools=['skills_list', 'skill_view'],
    )
    from hermes_cli.main import main as hermes_main
    hermes_main()


if __name__ == '__main__':
    main()
