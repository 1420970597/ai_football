"""防止人工计划再次被数量模板、错误依赖和无证据勾选破坏。"""

import unittest

from tasks.check_planning import validate


CHARTER = "# 中文工程约束\n\n所有规划必须描述真实功能、实现路径和失败验收，不设固定任务数量。\n\n| `docs/` | 项目说明 | 禁止秘密 |\n"


def package(identifier="P01", dependency="无", action="核对归档恢复内容并保存可追溯校验结果。", checked=False):
    return f"""### {identifier} 恢复验证

责任目录：docs/。依赖：{dependency}。当前：未验收。产出：恢复证据。

研究与设计：区分归档存在和业务可恢复。

实现路径：隔离恢复样本并对比哈希。

验收与失败：不一致保留原件并停止清理。

- [{'x' if checked else ' '}] {identifier}-01 {action}
"""


class PlanningTests(unittest.TestCase):
    def test_one_package_is_valid_without_fixed_task_count(self):
        result = validate(package(), CHARTER, "")
        self.assertEqual((), result.errors)
        self.assertEqual((1, 1, 0), (result.packages, result.actions, result.completed))

    def test_duplicate_actions_rejected(self):
        result = validate(package() + package("P02"), CHARTER, "")
        self.assertTrue(any("动作内容重复" in error for error in result.errors))

    def test_long_template_with_changed_subject_rejected(self):
        common = "必须记录参与者输入输出所有权限边界完整操作步骤实际命令退出码及失败重试和回滚处理证据才能宣称完成"
        plan = "".join(package(f"P{i:02}", action=f"研究第{i}业务功能并且{common}") for i in range(1, 4))
        result = validate(plan, CHARTER, "")
        self.assertTrue(any("模板重复" in error for error in result.errors))

    def test_unknown_and_cyclic_dependencies_rejected(self):
        result = validate(package(dependency="P99"), CHARTER, "")
        self.assertTrue(any("不存在依赖" in error for error in result.errors))
        plan = package(dependency="P02") + package("P02", dependency="P01", action="确认对象引用后演练媒体恢复。")
        self.assertTrue(any("循环" in error for error in validate(plan, CHARTER, "").errors))

    def test_missing_implementation_or_failure_definition_rejected(self):
        plan = package().replace("实现路径：", "待考虑：").replace("验收与失败：", "说明：")
        errors = validate(plan, CHARTER, "").errors
        self.assertTrue(any("缺少实现路径" in error for error in errors))
        self.assertTrue(any("缺少验收与失败" in error for error in errors))

    def test_done_requires_linked_evidence(self):
        self.assertTrue(validate(package(checked=True), CHARTER, "").errors)
        plan = package(action="隔离恢复校验一致。（证据：E01）", checked=True)
        self.assertEqual((), validate(plan, CHARTER, "E01：容器验证退出码 0。").errors)
        self.assertTrue(validate(plan, CHARTER, "无对应证据。").errors)

    def test_filename_instead_of_directory_rejected(self):
        self.assertTrue(any("具体文件名" in e for e in validate(package() + "\n参考 config.py", CHARTER, "").errors))

    def test_unknown_directory_rejected(self):
        result = validate(package().replace("docs/", "secret-stuff/"), CHARTER, "")
        self.assertTrue(any("未声明目录职责" in error for error in result.errors))

    def test_dependency_range_includes_middle_packages(self):
        result = validate(package("P04", dependency="P01 至 P03"), CHARTER, "")
        self.assertTrue(any("P02" in error for error in result.errors))

    def test_validation_has_no_write_effect(self):
        plan = package()
        before = (plan, CHARTER)
        validate(plan, CHARTER, "")
        self.assertEqual(before, (plan, CHARTER))


if __name__ == "__main__":
    unittest.main()
