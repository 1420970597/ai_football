"""只读检查人工规划；不生成任务，也不按固定数量判定质量。"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import re
import sys


PACKAGE = re.compile(r"^### (P\d{2,}) (.+)$", re.MULTILINE)
ACTION = re.compile(r"^- \[([ xX])\] (P\d{2,}-\d{2,}) (.+)$", re.MULTILINE)
FILE_NAME = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z_][A-Za-z0-9_.-]*\.(?:md|py|json|ya?ml|toml|sql|js|ts|html|css|env|gradle|xml)\b",
    re.IGNORECASE,
)
REQUIRED_FIELDS = ("责任目录：", "依赖：", "当前：", "产出：", "研究与设计：", "实现路径：", "验收与失败：")
SHARED_PHRASE_LENGTH = 32


@dataclass(frozen=True)
class Result:
    packages: int
    actions: int
    completed: int
    errors: tuple[str, ...]


def dependencies(text: str) -> set[str]:
    result = set(re.findall(r"P\d{2,}", text))
    for start, end in re.findall(r"P(\d{2,})\s*至\s*P(\d{2,})", text):
        if int(start) > int(end):
            raise ValueError("依赖范围倒置")
        result.update(f"P{number:02}" for number in range(int(start), int(end) + 1))
    return result


def validate(plan: str, charter: str, progress: str) -> Result:
    errors: list[str] = []
    headings = list(PACKAGE.finditer(plan))
    package_ids = [match[1] for match in headings]
    if not headings:
        errors.append("没有可识别工作包")
    if len(package_ids) != len(set(package_ids)):
        errors.append("工作包编号重复")
    for label, content in (("规划", plan), ("工程约束", charter)):
        names = sorted(set(FILE_NAME.findall(content)))
        if names:
            errors.append(f"{label}出现具体文件名：{', '.join(names)}")
        if len(re.findall(r"[\u4e00-\u9fff]", content)) <= len(re.findall(r"[A-Za-z]{3,}", content)):
            errors.append(f"{label}应以中文说明")

    allowed = set(re.findall(r"\| `([^`]+/)` \|", charter))
    allowed |= {part[:index + 1] for part in tuple(allowed) for index, char in enumerate(part) if char == "/"}
    graph: dict[str, set[str]] = {}
    ids: set[str] = set()
    texts: dict[str, str] = {}
    phrases: dict[str, set[str]] = defaultdict(set)
    action_count = completed_count = 0

    for index, match in enumerate(headings):
        package_id = match[1]
        end = headings[index + 1].start() if index + 1 < len(headings) else len(plan)
        body = plan[match.end():end]
        for field in REQUIRED_FIELDS:
            if field not in body:
                errors.append(f"{package_id}缺少{field}")
        scope = re.search(r"责任目录：([^。\n]+)", body)
        if scope:
            for directory in re.findall(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)*/", scope[1]):
                if directory not in allowed:
                    errors.append(f"{package_id}未声明目录职责：{directory}")
        dependency_text = re.search(r"依赖：([^。\n]+)", body)
        try:
            graph[package_id] = dependencies(dependency_text[1]) if dependency_text else set()
        except ValueError as exc:
            errors.append(f"{package_id}{exc}")
            graph[package_id] = set()
        actions = list(ACTION.finditer(body))
        if not actions:
            errors.append(f"{package_id}没有可执行动作")
        for action in actions:
            status, action_id, description = action.groups()
            action_count += 1
            if not action_id.startswith(package_id + "-"):
                errors.append(f"{action_id}归属工作包错误")
            if action_id in ids:
                errors.append(f"动作编号重复：{action_id}")
            ids.add(action_id)
            if status.lower() == "x":
                completed_count += 1
                evidence = re.findall(r"证据：([A-Z]\d{2,})", description)
                if not evidence or any(not re.search(rf"\b{re.escape(item)}\b", progress) for item in evidence):
                    errors.append(f"{action_id}已勾选但缺少进度证据")
            description = re.sub(r"（证据：[^）]+）", "", description)
            normalized = re.sub(r"[\W\d_]+", "", description).lower()
            if normalized in texts:
                errors.append(f"动作内容重复：{texts[normalized]}、{action_id}")
            texts[normalized] = action_id
            for offset in range(len(normalized) - SHARED_PHRASE_LENGTH + 1):
                phrases[normalized[offset:offset + SHARED_PHRASE_LENGTH]].add(package_id)
    repeated = set()
    for owners in phrases.values():
        if len(owners) >= 3:
            repeated.add(tuple(sorted(owners)))
    for owners in sorted(repeated):
        errors.append(f"跨工作包存在长段模板重复：{'、'.join(owners)}；需要人工复查")

    for package_id, required in graph.items():
        unknown = required - set(package_ids)
        if unknown:
            errors.append(f"{package_id}引用不存在依赖：{'、'.join(sorted(unknown))}")
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(package_id: str) -> None:
        if package_id in visiting:
            errors.append(f"依赖存在循环：{package_id}")
            return
        if package_id in visited:
            return
        visiting.add(package_id)
        for required in sorted(graph.get(package_id, set())):
            walk(required)
        visiting.remove(package_id)
        visited.add(package_id)

    for package_id in graph:
        walk(package_id)
    return Result(len(headings), action_count, completed_count, tuple(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = args.root
    try:
        result = validate(
            (root / "TODO.md").read_text(encoding="utf-8"),
            (root / "AGENTS.md").read_text(encoding="utf-8"),
            (root / "docs/platform-v2/PROGRESS.md").read_text(encoding="utf-8"),
        )
    except (OSError, UnicodeError) as exc:
        print(f"规划读取失败：{type(exc).__name__}", file=sys.stderr)
        return 2
    print(f"工作包 {result.packages}，动作 {result.actions}，有证据完成项 {result.completed}")
    for error in result.errors:
        print(error, file=sys.stderr)
    print("只读结构检查通过；语义质量与实际验收仍须人工核验。" if not result.errors else "检查未通过。")
    return int(bool(result.errors))


if __name__ == "__main__":
    raise SystemExit(main())
