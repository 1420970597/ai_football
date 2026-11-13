"""Run one independent, audited expert-review agent per closed UTC window.

The supervisor never queries business data or deploys services itself. It owns
the cadence, serial lease, bounded child process, private evidence retention,
explicit analysis gaps and independent repair backlog. The agent follows the project's review/release
contracts. Credentials are injected as read-only files, never copied or logged.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Callable

if __package__:
    from . import scan_scheduler as scheduler
else:
    import scan_scheduler as scheduler

UTC = timezone.utc
PERIOD_SECONDS = 1800
RAW_RETENTION_SECONDS = 72 * 60 * 60
MAX_EVENT_BYTES = 1024 * 1024
EVENT_TYPES = frozenset({"thread.started", "turn.started", "turn.completed", "turn.failed",
                         "item.started", "item.updated", "item.completed", "error", "warning"})
ITEM_TYPES = frozenset({"agent_message", "reasoning", "command_execution", "mcp_tool_call",
                        "file_change", "web_search", "plan", "error", "collab_tool_call"})
EVENT_STATUSES = frozenset({"in_progress", "completed", "failed", "interrupted", "cancelled",
                           "queued", "running", "success", "errored"})
SAFE_EVENT_ID = re.compile(r"(?:item_[0-9]{1,12}|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})\Z")
BLOCKER_CODES = frozenset({"media_unavailable", "evidence_incomplete", "provider_unavailable", "upstream_unavailable",
                           "validation_failed", "release_blocked", "no_increment", "timeout", "read_failed", "accuracy_uncertain"})


def utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str) -> int:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("UTC timestamp must include an offset")
    return int(parsed.timestamp())


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        os.chmod(temporary, 0o600)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


@dataclass(frozen=True)
class Config:
    source_root: Path
    state_dir: Path
    first_start: int
    codex_binary: str = "/usr/local/bin/codex"
    codex_home: Path = Path("/run/expert-codex-home")
    schema_path: Path = Path("/opt/expert-monitor/result-schema.json")
    timeout_seconds: int = 600
    retry_seconds: int = 300
    project_root: Path | None = None
    repair_timeout_seconds: int = 600
    heartbeat: Callable | None = None

    @classmethod
    def from_environment(cls) -> "Config":
        config = cls(
            source_root=Path(os.environ["EXPERT_SOURCE_ROOT"]).resolve(),
            state_dir=Path(os.environ["EXPERT_ITERATION_STATE_DIR"]).resolve(),
            first_start=parse_timestamp(os.environ.get(
                "EXPERT_ITERATION_FIRST_START", "2026-11-12T13:00:00Z")),
            codex_binary=os.environ.get("EXPERT_CODEX_BINARY", "/usr/local/bin/codex"),
            codex_home=Path(os.environ.get("CODEX_HOME", "/run/expert-codex-home")),
            schema_path=Path(os.environ.get(
                "EXPERT_ITERATION_SCHEMA", "/opt/expert-monitor/result-schema.json")),
            timeout_seconds=int(os.environ.get("EXPERT_ITERATION_TIMEOUT_SECONDS", "600")),
            retry_seconds=int(os.environ.get("EXPERT_ITERATION_RETRY_SECONDS", "300")),
            project_root=Path(os.environ["EXPERT_PROJECT_ROOT"]).resolve(),
            repair_timeout_seconds=int(os.environ.get("EXPERT_REPAIR_TIMEOUT_SECONDS", "600")),
        )
        if config.first_start % PERIOD_SECONDS:
            raise ValueError("First window must start on a UTC half-hour boundary")
        if not 1 <= config.timeout_seconds <= PERIOD_SECONDS:
            raise ValueError("Agent timeout must be between 1 and 1800 seconds")
        if not 1 <= config.retry_seconds <= PERIOD_SECONDS:
            raise ValueError("Retry delay must be between 1 and 1800 seconds")
        if not 60 <= config.repair_timeout_seconds <= 900:
            raise ValueError("Repair timeout must be between 60 and 900 seconds")
        if not config.source_root.is_dir():
            raise ValueError("Authoritative source tree does not exist")
        # Do not allow a typo to place raw expert content in Git-visible reports.
        if ".runtime" not in config.state_dir.parts:
            raise ValueError("Private state must be inside the project's .runtime directory")
        if not config.source_root.is_relative_to(config.project_root) or not config.state_dir.is_relative_to(config.project_root):
            raise ValueError("Source and private state must stay inside the designated project")
        # Migrate legacy 1800s settings without allowing a scan to hold the lane.
        return replace(config, timeout_seconds=min(config.timeout_seconds, scheduler.SCAN_BUDGET))


class AlreadyRunning(RuntimeError):
    pass


def acquire_lock(state_dir: Path):
    state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(state_dir, 0o700)
    descriptor = (state_dir / "supervisor.lock").open("a+")
    try:
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        descriptor.close()
        raise AlreadyRunning("Another expert-iteration supervisor owns this state directory") from error
    return descriptor


def load_progress(config: Config) -> dict:
    path = config.state_dir / "progress.json"
    if not path.exists():
        return {"schema_version": 1, "first_start": config.first_start,
                "finished_watermark": config.first_start}
    progress = json.loads(path.read_text())
    if progress.get("first_start") != config.first_start:
        raise ValueError("Configured first window differs from persisted schedule")
    watermark = progress.get("finished_watermark")
    if not isinstance(watermark, int) or watermark < config.first_start or watermark % PERIOD_SECONDS:
        raise ValueError("Invalid persisted success watermark")
    return progress


def next_window(progress: dict, now: float) -> tuple[int, int] | None:
    start = progress["finished_watermark"]
    end = start + PERIOD_SECONDS
    return (start, end) if end <= now else None


def make_prompt(config: Config, start: int, end: int, attempt_dir: Path, task: dict | None = None) -> str:
    task = task or {"kind": "scan"}
    phase = ("本轮只读扫描：禁止改业务、Git提交/推送、重复planning或更新历史进度文档。"
             "只读取最新短handoff、上一窗结构化结果及本窗新样本，不遍历数十篇旧报告。"
             "扫描完成与修复完成分开：全部可访问样本已核验时analysis_status=complete；"
             "缺图片/证据不足时用partial，读取整体失败用failed。媒体或修复blocked不能把已做分析说成未做。"
             "现存相同原图/unsupported已核验但证据仍不可判定时partial、retryable=false，保留缺口等待新证据，禁止重复烧同图。"
             "确认缺陷且有最小失败复现才填写repair_candidates；issue_key为稳定缺陷标识SHA256，"
             "evidence_fingerprint只来自新样本版本、阻塞状态与失败断言，不包含时间/重试号/文档/HEAD。"
             "无新增证据不得重复生成planning、文档或提交。扫描只写本次私有runtime。"
             if task["kind"] == "scan" else
             f"本轮独立修复：只处理已确认缺陷issue_key={task['issue_key']}，"
             f"evidence_fingerprint={task['evidence_fingerprint']}；"
             f"前次最小复现位于本窗attempt={task['analysis_attempt']}的私有证据。"
             "先核对最小复现和实际源代码，确认真实漏配/延迟原因后最小修改对应审核/采集/监控代码；"
             "添加针对性回归、冻结后Docker验证，对准确率有影响时复验相关正负例与隔离原图。"
             "按现有门禁串行准备、验收并部署；发布受阻保留待办，不妨碍后续扫描。"
             "仅真实业务/测试源修复才提交必要源码和简短说明；无代码增量禁止planning/doc空提交。"
             "稳定外部阻塞用repair_status=blocked、retryable=false，等待新证据，不反复调用自己。"
             "validated必须有真实源码提交、validation_passed=true及验收证据；不能用文档提交当修复。"
             "本轮analysis_status=not_run，不重扫全站或历史旧窗。")
    return f"""你是用户已明确授权的球讯项目专家审核迭代子代理。仅处理UTC半开窗口
[{utc_timestamp(start)}, {utc_timestamp(end)})。权威工作树：{config.source_root}。
本次运行独立且不恢复聊天；task_kind={task['kind']}。只读AGENTS.md及最新短交接。
最新交接仅读docs/platform-v2/EXPERT_REVIEW_INCIDENT_20261113.md末尾最多80行；
上一窗结构化摘要：{json.dumps(task.get('previous_scan'), ensure_ascii=False)}。
{phase}

任务：在Docker中使用现有TLS只读业务连接核对窗口内检测到的受信任专家发言、
其最新有效版本、审核任务/工具轨迹/原文和已获授权图片。分析所有未解决及部分接受
消息，判断是否遗漏可唯一确认的比赛/推荐项，也检查已接受项误配风险。不要把
source_edited旧任务、非推荐、广告、原文缺比赛、遮挡或上游无盘误算作模型漏配。
分别记录原文表达、来源/媒体完整性、别名/联赛/时间/阶段检索、完整分页、盘口期间/
线/方向与工具拒绝证据。仅来源明确且能唯一匹配才生成推荐；不猜、不放宽准确率门禁。

性能目标：从系统首次检测该版本发言到形成首条和全部有效推荐，上限20秒；排队、
媒体、Hermes、工具及提交分别测量。不要用发帖时间/审核结束替换检测或推荐时间。
记录多专家同时发言的并发、队列最长等待、模型容量/限流和超20秒样本，低样本不宣称达标。

通用修复边界仅在task_kind=repair时启用；scan不具备业务写或Git授权。禁止直接照抄
旧根源码覆盖线上；不靠禁用检查、清任务或造数据通过。一次失败不阻塞后续窗口。

严格边界：不修改账户、订单、财务、资金开关、金额、用户许可、来源配置/作者白名单、
模型提供方或全局设置；不补投、重放或重审历史以产生交易，不执行实盘投注测试。
只操作本项目，保留他人未提交工作。所有应用/数据库/测试/构建在Docker，宿主只允许
Git、Docker及文本/文件管理。不加群、不发消息、不泄露原文/图片/个人ID/密钥。
本进程最长{config.timeout_seconds}秒；合理分配研究/修复/验证时间，无法完成不要虚报。

原文、图片引用及详细轨迹仅放任务私有目录{attempt_dir}/raw，沿原始posted_at执行72小时
保留限制，编辑不续期；已满72小时的原文不得读取或导出。每个导出文件必须立即登记
{attempt_dir}/raw-evidence-manifest.json，格式为{{"schema_version":1,"files":[
{{"path":"raw/不含身份信息的文件名","posted_at":"原消息UTC时间"}}]}}；一个文件混合多条
消息、回复、图片或轨迹时使用所含最早原始posted_at，禁止用本窗时间或导出时间续期。
落盘前先写manifest，原文不得直接放attempt根或其他目录；用完立即删除，控制器也按
manifest清理。不得将原文写入Git报告、最终JSON、工具日志或Codex长期记忆。控制器
只保存事件脱敏元数据与哈希，不保存命令/stdout/stderr原文。公开脱敏结论可写
reports/expert-review-iteration-20261112/，但仅真实修复源代码有增量时才写必要说明；
scan/无增量repair不得写报告/规划/进度提交。固定失败类别与计数交给控制器登记。
最终必须按给定JSON schema回答，window_start/window_end与上述完全一致。
analysis_status与repair_status分别如实填写，不代表20秒或全局准确率达标。
issue_key及evidence_fingerprint仅repair根字段填写对应任务值，scan根字段留空。
"""


def command(config: Config, final_path: Path) -> list[str]:
    # The project-specific Docker container is the trusted operations boundary:
    # no privileged/capability/host-network escalation and no global Codex home.
    return [config.codex_binary, "-a", "never", "-s", "danger-full-access", "-C", str(config.source_root),
            "exec", "--ephemeral", "--json", "--color", "never",
            "--output-schema", str(config.schema_path), "-o", str(final_path), "-"]


def commit_success(config: Config, progress: dict, record: dict) -> bool:
    """Advance only the next contiguous window after an audited success."""
    start = progress["finished_watermark"]
    if record.get("state") != "succeeded" or record.get("exit_code") != 0:
        return False
    if (record.get("window_start"), record.get("window_end")) != (
            utc_timestamp(start), utc_timestamp(start + PERIOD_SECONDS)):
        raise ValueError("Success record is not the next contiguous window")
    progress["finished_watermark"] = start + PERIOD_SECONDS
    progress["last_success_at"] = record["finished_at"]
    atomic_json(config.state_dir / "progress.json", progress)
    return True


def recover_recorded_success(config: Config, progress: dict) -> bool:
    """A crash after saving success but before watermark must not repeat writes."""
    path = config.state_dir / "windows" / f'{progress["finished_watermark"]}.json'
    if not path.exists():
        return False
    return commit_success(config, progress, json.loads(path.read_text()))


class RedactedOutput:
    """Bounded in-memory parsing; model/tool text is never sent to disk."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.output = (directory / "events.jsonl").open("w", encoding="utf-8")
        os.chmod(directory / "events.jsonl", 0o600)
        self.stdout_hash, self.stderr_hash, self.line_hash = (hashlib.sha256() for _ in range(3))
        self.stdout_bytes = self.stderr_bytes = self.line_bytes = self.events = 0
        self.line = bytearray()
        self.stderr_tail = b""
        self.failures = set()

    def _event(self):
        record = {"bytes": self.line_bytes, "sha256": self.line_hash.hexdigest()}
        try:
            event = json.loads(self.line) if self.line_bytes <= MAX_EVENT_BYTES else None
        except (ValueError, UnicodeDecodeError):
            event = None
        if not isinstance(event, dict):
            record["type"] = "oversized_event" if self.line_bytes > MAX_EVENT_BYTES else "invalid_json"
        else:
            kind = event.get("type")
            record["type"] = kind if isinstance(kind, str) and kind in EVENT_TYPES else "unknown_event"
            item = event.get("item") if isinstance(event.get("item"), dict) else {}
            if isinstance(item.get("type"), str) and item["type"] in ITEM_TYPES:
                record["item_type"] = item["type"]
            identifier = item.get("id", event.get("thread_id", event.get("id")))
            if isinstance(identifier, str) and SAFE_EVENT_ID.fullmatch(identifier):
                record["id"] = identifier
            status = item.get("status", event.get("status"))
            if isinstance(status, str) and status in EVENT_STATUSES:
                record["status"] = status
            exit_code = item.get("exit_code", event.get("exit_code"))
            if type(exit_code) is int:
                record["exit_code"] = exit_code
        self.output.write(json.dumps(record, separators=(",", ":")) + "\n")
        self.output.flush()
        self.events += 1
        self.line.clear()
        self.line_hash = hashlib.sha256()
        self.line_bytes = 0

    def feed(self, name: str, data: bytes):
        if name == "stderr":
            self.stderr_hash.update(data)
            self.stderr_bytes += len(data)
            # Keep only a tiny overlap for split failure markers, never the text.
            sample = (self.stderr_tail + data).lower()
            for marker, category in ((b"bwrap", "sandbox_unavailable"),
                                     (b"codex-code-mode-host", "code_mode_host_unavailable"),
                                     (b"permission denied", "permission_denied"),
                                     (b"unauthorized", "authentication_failed"),
                                     (b"connection refused", "provider_unavailable")):
                if marker in sample:
                    self.failures.add(category)
            self.stderr_tail = sample[-64:]
            return
        self.stdout_hash.update(data)
        self.stdout_bytes += len(data)
        parts = data.split(b"\n")
        for position, part in enumerate(parts):
            complete = position < len(parts) - 1
            segment = part + (b"\n" if complete else b"")
            self.line_hash.update(segment)
            self.line_bytes += len(segment)
            if len(self.line) < MAX_EVENT_BYTES:
                self.line.extend(segment[:MAX_EVENT_BYTES - len(self.line)])
            if complete:
                self._event()

    def close(self):
        if self.line_bytes:
            self._event()
        self.output.close()
        atomic_json(self.directory / "output-summary.json", {
            "schema_version": 2, "redacted": True, "events": self.events,
            "stdout": {"bytes": self.stdout_bytes, "sha256": self.stdout_hash.hexdigest()},
            "stderr": {"bytes": self.stderr_bytes, "sha256": self.stderr_hash.hexdigest(),
                       "failure_categories": sorted(self.failures)}})
        self.line.clear()
        self.stderr_tail = b""


def capture_output(process, prompt: bytes, output: RedactedOutput, timeout: float, maintenance=None):
    """Drain both pipes without deadlocks or unbounded communicate() buffers."""
    deadline = time.monotonic() + timeout
    next_maintenance = time.monotonic() + 15
    with selectors.DefaultSelector() as poller:
        for stream, label in ((process.stdout, "stdout"), (process.stderr, "stderr"),
                              (process.stdin, "stdin")):
            os.set_blocking(stream.fileno(), False)
            poller.register(stream, selectors.EVENT_WRITE if label == "stdin" else selectors.EVENT_READ, label)
        sent = 0
        while poller.get_map():
            if maintenance is not None and time.monotonic() >= next_maintenance:
                maintenance()
                next_maintenance = time.monotonic() + 15
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired("expert-iteration-child", timeout)
            for key, _ in poller.select(min(remaining, 1)):
                if key.data == "stdin":
                    try:
                        sent += os.write(key.fd, prompt[sent:sent + 65536])
                    except BrokenPipeError:
                        sent = len(prompt)
                    if sent == len(prompt):
                        poller.unregister(key.fileobj)
                        key.fileobj.close()
                    continue
                data = os.read(key.fd, 65536)
                if not data:
                    poller.unregister(key.fileobj)
                    key.fileobj.close()
                else:
                    output.feed(key.data, data)
        process.wait(timeout=max(.001, deadline - time.monotonic()))


def redact_final(source: Path, destination: Path):
    """The schema answer lives on tmpfs; persist counters and hashes only."""
    digest, size, content = hashlib.sha256(), 0, bytearray()
    try:
        with source.open("rb") as stream:
            while data := stream.read(65536):
                digest.update(data)
                size += len(data)
                if size <= MAX_EVENT_BYTES:
                    content.extend(data)
        result = json.loads(content) if size <= MAX_EVENT_BYTES else None
    except (OSError, ValueError):
        result = None
    redacted = {"schema_version": 2, "redacted": True, "bytes": size,
                "sha256": digest.hexdigest(), "status": "invalid", "summary": ""}
    if isinstance(result, dict):
        for key in ("window_start", "window_end"):
            value = result.get(key)
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", value):
                redacted[key] = value
        status = result.get("status")
        redacted["status"] = status if isinstance(status, str) and status in {"complete", "blocked", "needs_retry"} else "invalid"
        for key in ("messages_examined", "unresolved_examined", "confirmed_omissions", "confirmed_misbindings"):
            value = result.get(key)
            redacted[key] = value if type(value) is int else None
        if type(result.get("latency_target_met")) is bool:
            redacted["latency_target_met"] = result["latency_target_met"]
        for key, allowed in (("task_kind", {"scan", "repair"}),
                             ("analysis_status", {"complete", "partial", "failed", "not_run"}),
                             ("repair_status", {"not_needed", "pending", "blocked", "validated", "failed"})):
            value = result.get(key)
            if isinstance(value, str) and value in allowed:
                redacted[key] = value
        for key in ("issue_key", "evidence_fingerprint"):
            value = result.get(key)
            if value == "" or scheduler.is_hash(value):
                redacted[key] = value
        for key in ("incremental_evidence", "retryable", "validation_passed"):
            if type(result.get(key)) is bool:
                redacted[key] = result[key]
        candidates = result.get("repair_candidates")
        if isinstance(candidates, list) and len(candidates) <= 50:
            valid = all(isinstance(item, dict) and set(item) == {"issue_key", "evidence_fingerprint", "minimal_reproduction", "defect_count"}
                        and scheduler.is_hash(item["issue_key"]) and scheduler.is_hash(item["evidence_fingerprint"])
                        and type(item["minimal_reproduction"]) is bool and type(item["defect_count"]) is int
                        and item["defect_count"] > 0 for item in candidates)
            if valid:
                redacted["repair_candidates"] = candidates
        blockers = result.get("blocker_codes")
        if isinstance(blockers, list) and len(blockers) <= 12 and all(isinstance(value, str) and value in BLOCKER_CODES for value in blockers):
            redacted["blocker_codes"] = blockers
        commits = result.get("fix_commits")
        if isinstance(commits, list) and len(commits) <= 20 and all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) for value in commits):
            redacted["fix_commits"] = commits
        summary = result.get("summary")
        if isinstance(summary, str) and summary.strip():
            redacted["summary"] = "Agent supplied a summary; free text withheld."
            redacted["summary_bytes"] = len(summary.encode(errors="replace"))
            redacted["summary_sha256"] = hashlib.sha256(summary.encode(errors="replace")).hexdigest()
    atomic_json(destination, redacted)


def run_child(config: Config, prompt: str, attempt_dir: Path) -> tuple[int, bool]:
    environment = {**os.environ, "CODEX_HOME": str(config.codex_home), "EXPERT_ITERATION_CHILD": "1"}
    # /tmp is an explicit bounded tmpfs in the controller Compose service.
    with tempfile.TemporaryDirectory(prefix="expert-result-") as scratch:
        final_path = Path(scratch) / "final.json"
        output = RedactedOutput(attempt_dir)
        process = None
        grace = min(10, config.timeout_seconds / 10)
        try:
            process = subprocess.Popen(command(config, final_path), stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       env=environment, start_new_session=True, bufsize=0)
            capture_output(process, prompt.encode(), output, config.timeout_seconds - grace,
                           lambda: (prune_private_evidence(config, time.time()), config.heartbeat() if config.heartbeat else None))
            return process.returncode, False
        except subprocess.TimeoutExpired:
            if process is not None:
                terminate_child(process, grace)
            return 124, True
        except BaseException:
            # A stop/restart must not leave a writer running outside our lock.
            if process is not None:
                terminate_child(process, 10)
            raise
        finally:
            if process is not None:
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()
            output.close()
            redact_final(final_path, attempt_dir / "final.json")


def terminate_child(process, grace: float):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def validate_result(path: Path, start: int, end: int) -> tuple[bool, str]:
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        return False, "missing_or_invalid_final_json"
    if result.get("window_start") != utc_timestamp(start) or result.get("window_end") != utc_timestamp(end):
        return False, "result_window_mismatch"
    if result.get("status") != "complete":
        status = result.get("status")
        return False, "agent_" + status if status in {"blocked", "needs_retry"} else "agent_invalid_status"
    expected_counts = ("messages_examined", "unresolved_examined", "confirmed_omissions", "confirmed_misbindings")
    if any(type(result.get(key)) is not int or result[key] < 0 for key in expected_counts):
        return False, "invalid_result_counts"
    if result["unresolved_examined"] > result["messages_examined"]:
        return False, "invalid_result_counts"
    if not isinstance(result.get("summary"), str) or not result["summary"].strip():
        return False, "missing_result_summary"
    return True, "complete"


def validate_task_result(path: Path, task: dict) -> tuple[bool, str, dict]:
    try:
        result = json.loads(path.read_text())
    except (OSError, ValueError):
        return False, "missing_or_invalid_final_json", {}
    if not isinstance(result, dict) or result.get("window_start") != utc_timestamp(task["start"]) or result.get("window_end") != utc_timestamp(task["end"]):
        return False, "result_window_mismatch", {}
    keys = ("messages_examined", "unresolved_examined", "confirmed_omissions", "confirmed_misbindings")
    if any(type(result.get(key)) is not int or result[key] < 0 for key in keys) or result["unresolved_examined"] > result["messages_examined"]:
        return False, "invalid_result_counts", {}
    if result.get("task_kind") != task["kind"]:
        return False, "result_task_mismatch", {}
    if not all(type(result.get(key)) is bool for key in ("retryable", "validation_passed", "incremental_evidence", "latency_target_met")):
        return False, "invalid_result_flags", {}
    blockers, candidates = result.get("blocker_codes"), result.get("repair_candidates")
    if not isinstance(blockers, list) or len(blockers) > 12 or not all(isinstance(code, str) and code in BLOCKER_CODES for code in blockers):
        return False, "invalid_blocker_codes", {}
    if not isinstance(candidates, list) or len(candidates) > 50 or not all(
            isinstance(item, dict) and set(item) == {"issue_key", "evidence_fingerprint", "minimal_reproduction", "defect_count"}
            and scheduler.is_hash(item["issue_key"]) and scheduler.is_hash(item["evidence_fingerprint"])
            and type(item["minimal_reproduction"]) is bool and type(item["defect_count"]) is int and item["defect_count"] > 0
            for item in candidates):
        return False, "invalid_repair_candidates", {}
    if task["kind"] == "scan":
        if result.get("analysis_status") not in {"complete", "partial", "failed"}:
            return False, "invalid_analysis_status", {}
        if result.get("repair_status") not in {"not_needed", "pending", "blocked"}:
            return False, "scan_cannot_validate_repairs", {}
    else:
        if result.get("analysis_status") != "not_run" or result.get("issue_key") != task["issue_key"] or result.get("evidence_fingerprint") != task["evidence_fingerprint"]:
            return False, "repair_evidence_mismatch", {}
        if result.get("repair_status") not in {"pending", "blocked", "validated", "failed"}:
            return False, "invalid_repair_status", {}
    commits = result.get("fix_commits")
    if not isinstance(commits, list) or len(commits) > 20 or not all(isinstance(value, str) and re.fullmatch(r"[a-f0-9]{40}", value) for value in commits):
        return False, "invalid_fix_commits", {}
    if result.get("repair_status") == "validated" and (not commits or not result["validation_passed"]):
        return False, "repair_validation_missing", {}
    retained = (*keys, "task_kind", "analysis_status", "repair_status", "issue_key", "evidence_fingerprint",
                "retryable", "validation_passed", "incremental_evidence", "latency_target_met", "blocker_codes", "repair_candidates", "fix_commits")
    return True, "analysis_" + result["analysis_status"] if task["kind"] == "scan" else "repair_" + result["repair_status"], {key: result.get(key) for key in retained}


def repository_revision(root: Path) -> str | None:
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, timeout=5, check=False)
        value = result.stdout.decode().strip()
        return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None


def has_source_commit(root: Path, before: str, after: str) -> bool:
    try:
        result = subprocess.run(["git", "-C", str(root), "diff", "--name-only", before, after], capture_output=True, timeout=5, check=False)
        prefixes = ("services/api/app/", "services/collector/", "integrations/hermes/skills/", "infra/monitor/")
        return result.returncode == 0 and any(path.startswith(prefixes) for path in result.stdout.decode().splitlines())
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return False


def prune_private_evidence(config: Config, now: float) -> int:
    """Message payload TTL is posted_at-based; metadata stays replayable."""
    attempts_root = config.state_dir / "attempts"
    if not attempts_root.exists():
        return 0
    removed = 0
    for window_dir in attempts_root.iterdir():
        if not window_dir.is_dir() or window_dir.is_symlink() or not window_dir.name.isdigit():
            continue
        for attempt_dir in window_dir.iterdir():
            if not attempt_dir.is_dir() or attempt_dir.is_symlink() or not attempt_dir.name.isdigit():
                continue
            removed += prune_manifest_evidence(attempt_dir, now)
            if now - int(window_dir.name) >= RAW_RETENTION_SECONDS:
                # Redacted controller logs have a separate operational cap.
                for name in ("events.jsonl", "stderr.log", "output-summary.json", "final.json", "prompt.txt"):
                    path = attempt_dir / name
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                        removed += 1
            # Never recursively remove unfamiliar agent/project content.
            if not any(attempt_dir.iterdir()):
                attempt_dir.rmdir()
        if not any(window_dir.iterdir()):
            window_dir.rmdir()
    return removed


def prune_manifest_evidence(attempt_dir: Path, now: float) -> int:
    """Delete only agent exports explicitly registered under this attempt/raw."""
    manifest_path = attempt_dir / "raw-evidence-manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return 0
    try:
        if manifest_path.stat().st_size > MAX_EVENT_BYTES:
            raise ValueError("manifest size")
        manifest = json.loads(manifest_path.read_text())
        files = manifest["files"]
        if manifest.get("schema_version") != 1 or not isinstance(files, list) or len(files) > 10000:
            raise ValueError("manifest contract")
        candidates = []
        for entry in files:
            if not isinstance(entry, dict) or set(entry) != {"path", "posted_at"}:
                raise ValueError("manifest entry")
            relative = Path(entry["path"])
            if relative.is_absolute() or not relative.parts or relative.parts[0] != "raw" or ".." in relative.parts:
                raise ValueError("manifest path")
            path = attempt_dir / relative
            # Avoid following any agent-created symlink out of its raw subtree.
            if any(parent.is_symlink() for parent in [path, *path.parents] if parent != attempt_dir.parent):
                raise ValueError("manifest symlink")
            if not path.resolve().is_relative_to((attempt_dir / "raw").resolve()):
                raise ValueError("manifest escape")
            stamp = parse_timestamp(entry["posted_at"])
            if stamp > now:
                raise ValueError("manifest future timestamp")
            candidates.append((entry, path, stamp))
        kept, removed = [], 0
        for entry, path, stamp in candidates:
            if now - stamp >= RAW_RETENTION_SECONDS:
                if path.is_file():
                    path.unlink()
                    removed += 1
            else:
                kept.append(entry)
        if len(kept) != len(files):
            atomic_json(manifest_path, {"schema_version": 1, "files": kept})
        (attempt_dir / "retention-status.json").unlink(missing_ok=True)
        return removed
    except (OSError, ValueError, KeyError, TypeError):
        atomic_json(attempt_dir / "retention-status.json", {
            "state": "failed", "category": "invalid_raw_evidence_manifest", "at": utc_timestamp(now)})
        return 0


def run_window(config: Config, start: int, end: int, *,
               clock: Callable[[], float] | None = None, runner: Callable = run_child,
               dry_run: bool = False, task: dict | None = None) -> dict:
    clock = time.time if clock is None else clock
    path = config.state_dir / "windows" / f"{start}.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    attempt = int(old.get("attempt", 0)) + 1
    attempt_dir = config.state_dir / "attempts" / str(start) / str(attempt)
    attempt_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    prompt = make_prompt(config, start, end, attempt_dir, task)
    (attempt_dir / "prompt.txt").write_text(prompt)
    record = {"schema_version": 1, "window_start": utc_timestamp(start), "window_end": utc_timestamp(end),
              "attempt": attempt, "started_at": utc_timestamp(clock()), "finished_at": None,
              "state": "dry_run" if dry_run else "running", "exit_code": None,
              "max_run_seconds": config.timeout_seconds, "previous_state": old.get("state")}
    if task:
        record.update(task_kind=task["kind"], task_token=task["token"])
    atomic_json(path, record)
    if task is None:
        atomic_json(config.state_dir / "status.json", record)
    if dry_run:
        return record
    before_revision = None
    try:
        before_revision = repository_revision(config.source_root) if task else None
        remaining = min(config.timeout_seconds, task["deadline_at"] - clock()) if task and "deadline_at" in task else config.timeout_seconds
        if remaining <= 0:
            exit_code, timed_out = 124, True
        else:
            exit_code, timed_out = runner(replace(config, timeout_seconds=remaining), prompt, attempt_dir)
        if task:
            valid, category, result = validate_task_result(attempt_dir / "final.json", task) if exit_code == 0 else (False, "child_failed", {})
            after_revision = repository_revision(config.source_root)
            changed = before_revision is not None and after_revision is not None and before_revision != after_revision
            if task["kind"] == "scan" and changed:
                valid, category = False, "scan_changed_repository"
            if task["kind"] == "repair" and result.get("repair_status") == "validated" and (
                    not changed or after_revision not in result.get("fix_commits", []) or not has_source_commit(config.source_root, before_revision, after_revision)):
                valid, category = False, "repair_has_no_source_commit"
            record.update(result_valid=valid, result=result, repository_changed=changed)
        else:
            valid, category = validate_result(attempt_dir / "final.json", start, end) if exit_code == 0 else (False, "child_failed")
        prune_manifest_evidence(attempt_dir, clock())
        if (attempt_dir / "retention-status.json").exists():
            valid, category = False, "invalid_raw_evidence_manifest"
        if task:
            record["result_valid"] = valid
        state = "succeeded" if valid else "timed_out" if timed_out else "failed"
        if task and valid:
            state = ("analysed" if result["analysis_status"] == "complete" else "partial" if result["analysis_status"] == "partial" else "analysis_failed") if task["kind"] == "scan" else "repaired" if result["repair_status"] == "validated" else "repair_pending"
        record.update({"finished_at": utc_timestamp(clock()), "exit_code": exit_code,
                       "state": state,
                       "category": category, "timed_out": timed_out})
    except KeyboardInterrupt:
        record.update(finished_at=utc_timestamp(clock()), state="interrupted", exit_code=130,
                      category="child_interrupted", result_valid=False)
    except Exception as error:
        record.update({"finished_at": utc_timestamp(clock()), "state": "failed", "exit_code": 1,
                       "category": "supervisor_child_" + type(error).__name__, "result_valid": False})
    atomic_json(path, record)
    # Retain every attempt's metadata even though raw evidence has a 72h cap.
    atomic_json(config.state_dir / "history" / f"{start}-{attempt}.json", record)
    return record


def load_schedule(config: Config, now: float) -> tuple[dict, dict]:
    path = config.state_dir / "schedule-v3.json"
    backlog_path = config.state_dir / "repair-backlog.json"
    state = json.loads(path.read_text()) if path.exists() else scheduler.initial_state(config.first_start)
    scheduler.validate_state(state, config.first_start)
    backlog = json.loads(backlog_path.read_text()) if backlog_path.exists() else {}
    if not isinstance(backlog, dict) or not all(scheduler.is_hash(key) and isinstance(value, dict)
            and value.get("issue_key") == key and scheduler.is_hash(value.get("evidence_fingerprint")) for key, value in backlog.items()):
        raise ValueError("invalid repair backlog")
    if not state["legacy_imported"]:
        # Validate the old cursor if present, but never rewrite it or infer that
        # blocked analyses succeeded. Original attempts/history stay untouched.
        load_progress(config)
        directory = config.state_dir / "windows"
        records = [json.loads(item.read_text()) for item in sorted(directory.glob("*.json"))] if directory.exists() else []
        scheduler.import_legacy(state, records, parse_timestamp, now)
        state["legacy_history_records"] = len(list((config.state_dir / "history").glob("*.json")))
    scheduler.enumerate_windows(state, now)
    active = state.get("active_task")
    if active:
        journal = config.state_dir / "windows" / f"{active['start']}.json"
        record = json.loads(journal.read_text()) if journal.exists() else {}
        if record.get("task_token") == active["token"] and record.get("finished_at") is not None:
            scheduler.finish_task(state, backlog, record, now, config.retry_seconds)
        else:
            scheduler.recover_interrupted(state, backlog, now, config.retry_seconds)
    else:
        scheduler.recover_interrupted(state, backlog, now, config.retry_seconds)
    return state, backlog


def save_schedule(config: Config, state: dict, backlog: dict):
    # Backlog first, then the task journal/cursor. Result replay is idempotent if
    # power is lost between these two atomic replacements.
    atomic_json(config.state_dir / "repair-backlog.json", backlog)
    atomic_json(config.state_dir / "schedule-v3.json", state)


def write_health(config: Config, state: dict, backlog: dict, now: float | None = None):
    now = time.time() if now is None else now
    scheduler.enumerate_windows(state, now)
    atomic_json(config.state_dir / "status.json", scheduler.health_snapshot(state, backlog, now, now))


def health_check(config: Config, now: float | None = None) -> int:
    now = time.time() if now is None else now
    try:
        status = json.loads((config.state_dir / "status.json").read_text())
        active = status.get("active_task")
        healthy = status.get("schema_version") == 3 and status.get("coverage_healthy") is True and now - status["heartbeat_at"] <= 90
        healthy = healthy and (active is None or now <= active["deadline_at"] + 10)
        return 0 if healthy else 1
    except (OSError, ValueError, KeyError, TypeError):
        return 1


def import_analysis(config: Config, state: dict, payload_path: Path, now: float) -> dict:
    """Explicit operator proof import; never reinterpret old failed records."""
    payload_path = payload_path.resolve()
    root = config.project_root or config.source_root
    if not payload_path.is_relative_to(root) or payload_path.stat().st_size > MAX_EVENT_BYTES:
        raise ValueError("analysis import must be a bounded project file")
    body = payload_path.read_bytes()
    payload = json.loads(body)
    if payload.get("schema_version") != 1 or not isinstance(payload.get("windows"), list) or not 1 <= len(payload["windows"]) <= 1000:
        raise ValueError("invalid analysis import")
    updates = []
    for item in payload["windows"]:
        start, end = parse_timestamp(item["window_start"]), parse_timestamp(item["window_end"])
        if str(start) not in state["windows"] or end != start + PERIOD_SECONDS or end > now:
            raise ValueError("import window not closed/enumerated")
        if item.get("analysis_status") not in {"complete", "partial"}:
            raise ValueError("import must report explicit analysis coverage")
        counts = {key: item.get(key) for key in ("messages_examined", "unresolved_examined")}
        if any(type(value) is not int or value < 0 for value in counts.values()) or counts["unresolved_examined"] > counts["messages_examined"]:
            raise ValueError("invalid import counts")
        original = Path(item["evidence_file"])
        evidence = original.resolve()
        if original.is_symlink() or not evidence.is_relative_to(root) or not evidence.is_file() or evidence.stat().st_size > 16 * MAX_EVENT_BYTES:
            raise ValueError("invalid import evidence file")
        digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
        if digest != item.get("evidence_sha256"):
            raise ValueError("analysis import evidence checksum differs")
        updates.append((start, {"analysis_status": item["analysis_status"], "manual_verified": True,
                               "manual_counts": counts, "manual_evidence_sha256": digest,
                               "manual_evidence_file": str(evidence.relative_to(root)), "manual_imported_at": now,
                               "exhausted": item["analysis_status"] != "complete", "retry_at": now + config.retry_seconds}))
    for start, fields in updates:
        state["windows"][str(start)].update(fields)
    scheduler.update_cursors(state)
    receipt = {"schema_version": 1, "payload_sha256": hashlib.sha256(body).hexdigest(),
               "windows": [start for start, _ in updates], "imported_at": now}
    atomic_json(config.state_dir / "imports" / f"{receipt['payload_sha256']}.json", receipt)
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one due window")
    parser.add_argument("--dry-run", action="store_true", help="Prepare one due window without launching an agent")
    parser.add_argument("--health", action="store_true", help="Check heartbeat, deadline and actual analysis coverage")
    parser.add_argument("--import-analysis", type=Path, help="Register hash-bound operator analysis; no agent/model calls")
    options = parser.parse_args()
    os.umask(0o077)
    config = Config.from_environment()
    if options.health:
        return health_check(config)
    with acquire_lock(config.state_dir):
        return run_supervisor(config, options)


def run_supervisor(config: Config, options) -> int:
    state, backlog = load_schedule(config, time.time())
    save_schedule(config, state, backlog)
    if options.import_analysis:
        receipt = import_analysis(config, state, options.import_analysis, time.time())
        save_schedule(config, state, backlog)
        write_health(config, state, backlog)
        print(json.dumps(receipt), flush=True)
        return 0

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        now = time.time()
        prune_private_evidence(config, now)
        task = scheduler.choose_task(state, backlog, now, config.timeout_seconds, config.repair_timeout_seconds)
        write_health(config, state, backlog, now)
        save_schedule(config, state, backlog)
        if task is None:
            if options.once or options.dry_run:
                return 0
            time.sleep(min(30, (int(now // PERIOD_SECONDS) + 1) * PERIOD_SECONDS - now))
            continue
        if options.dry_run:
            task["token"] = "dry-run"
            task["previous_scan"] = state.get("last_scan")
            record = run_window(config, task["start"], task["end"], dry_run=True, task=task)
            print(json.dumps(record), flush=True)
            return 0
        task["previous_scan"] = state.get("last_scan")
        scheduler.reserve_task(state, backlog, task, now, uuid.uuid4().hex)
        save_schedule(config, state, backlog)
        write_health(config, state, backlog, now)
        task = state["active_task"]
        runtime = replace(config, timeout_seconds=task["budget_seconds"], heartbeat=lambda: write_health(config, state, backlog))
        record = run_window(runtime, task["start"], task["end"], task=task)
        scheduler.finish_task(state, backlog, record, time.time(), config.retry_seconds)
        save_schedule(config, state, backlog)
        write_health(config, state, backlog)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        if record["exit_code"] == 130:
            return 130
        if options.once:
            return 0 if record["state"] in {"analysed", "repaired"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AlreadyRunning:
        print('{"state":"already_running","exit_code":3}', file=sys.stderr)
        raise SystemExit(3)
    except KeyboardInterrupt:
        print('{"state":"stopped","exit_code":130}', file=sys.stderr)
        raise SystemExit(130)
