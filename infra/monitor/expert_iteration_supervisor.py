"""Run one independent, audited expert-review agent per closed UTC window.

The supervisor never queries business data or deploys services itself. It owns
the cadence, lease, bounded child process, private evidence retention and
success watermark. The agent follows the project's existing review/release
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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

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
    timeout_seconds: int = PERIOD_SECONDS
    retry_seconds: int = 300
    project_root: Path | None = None

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
            timeout_seconds=int(os.environ.get("EXPERT_ITERATION_TIMEOUT_SECONDS", "1800")),
            retry_seconds=int(os.environ.get("EXPERT_ITERATION_RETRY_SECONDS", "300")),
            project_root=Path(os.environ["EXPERT_PROJECT_ROOT"]).resolve(),
        )
        if config.first_start % PERIOD_SECONDS:
            raise ValueError("First window must start on a UTC half-hour boundary")
        if not 1 <= config.timeout_seconds <= PERIOD_SECONDS:
            raise ValueError("Agent timeout must be between 1 and 1800 seconds")
        if not 1 <= config.retry_seconds <= PERIOD_SECONDS:
            raise ValueError("Retry delay must be between 1 and 1800 seconds")
        if not config.source_root.is_dir():
            raise ValueError("Authoritative source tree does not exist")
        # Do not allow a typo to place raw expert content in Git-visible reports.
        if ".runtime" not in config.state_dir.parts:
            raise ValueError("Private state must be inside the project's .runtime directory")
        if not config.source_root.is_relative_to(config.project_root) or not config.state_dir.is_relative_to(config.project_root):
            raise ValueError("Source and private state must stay inside the designated project")
        return config


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


def make_prompt(config: Config, start: int, end: int, attempt_dir: Path) -> str:
    return f"""你是用户已明确授权的球讯项目专家审核迭代子代理。仅处理UTC半开窗口
[{utc_timestamp(start)}, {utc_timestamp(end)})。权威工作树：{config.source_root}。
本次运行独立且不恢复聊天，先读AGENTS.md、最新交接和本工作包/上一窗脱敏结论。

任务：在Docker中使用现有TLS只读业务连接核对窗口内检测到的受信任专家发言、
其最新有效版本、审核任务/工具轨迹/原文和已获授权图片。分析所有未解决及部分接受
消息，判断是否遗漏可唯一确认的比赛/推荐项，也检查已接受项误配风险。不要把
source_edited旧任务、非推荐、广告、原文缺比赛、遮挡或上游无盘误算作模型漏配。
分别记录原文表达、来源/媒体完整性、别名/联赛/时间/阶段检索、完整分页、盘口期间/
线/方向与工具拒绝证据。仅来源明确且能唯一匹配才生成推荐；不猜、不放宽准确率门禁。

性能目标：从系统首次检测该版本发言到形成首条和全部有效推荐，上限20秒；排队、
媒体、Hermes、工具及提交分别测量。不要用发帖时间/审核结束替换检测或推荐时间。
记录多专家同时发言的并发、队列最长等待、模型容量/限流和超20秒样本，低样本不宣称达标。

默认只读调查。确认真实漏配/延迟根因后，用户已授权立即最小修复，但只能在本权威树
对应审核/采集/监控代码内修改；先记录可审查工作包及失败样本，添加针对性回归、冻结
后Docker验证。对准确率有潜在影响时复验相关既有正负例和真实原文/图片隔离样本。
按现有门禁准备、验收并顺序部署修复；禁止直接照抄旧根源码覆盖线上。无法满足验证
或发布条件时保留真实失败并标blocked，不靠禁用检查、清任务或造数据通过。

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
reports/expert-review-iteration-20261112/，记录窗口、数量、证据定位、根因、提交、
测试命令/退出码、发布状态和剩余限制。每阶段更新项目进度并按既有规则提交推送。
最终必须按给定JSON schema回答，window_start/window_end与上述完全一致。
status=complete只代表本窗分析及已确认修复验收完成，不代表20秒或全局准确率达标。
有未分析数据、无法访问图片、未验证修复或外部阻塞时用blocked/needs_retry，并说明。
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
                           lambda: prune_private_evidence(config, time.time()))
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
               clock: Callable[[], float] = time.time, runner: Callable = run_child,
               dry_run: bool = False) -> dict:
    path = config.state_dir / "windows" / f"{start}.json"
    old = json.loads(path.read_text()) if path.exists() else {}
    attempt = int(old.get("attempt", 0)) + 1
    attempt_dir = config.state_dir / "attempts" / str(start) / str(attempt)
    attempt_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    prompt = make_prompt(config, start, end, attempt_dir)
    (attempt_dir / "prompt.txt").write_text(prompt)
    record = {"schema_version": 1, "window_start": utc_timestamp(start), "window_end": utc_timestamp(end),
              "attempt": attempt, "started_at": utc_timestamp(clock()), "finished_at": None,
              "state": "dry_run" if dry_run else "running", "exit_code": None,
              "max_run_seconds": config.timeout_seconds, "previous_state": old.get("state")}
    atomic_json(path, record)
    atomic_json(config.state_dir / "status.json", record)
    if dry_run:
        return record
    try:
        exit_code, timed_out = runner(config, prompt, attempt_dir)
        valid, category = validate_result(attempt_dir / "final.json", start, end) if exit_code == 0 else (False, "child_failed")
        prune_manifest_evidence(attempt_dir, clock())
        if (attempt_dir / "retention-status.json").exists():
            valid, category = False, "invalid_raw_evidence_manifest"
        record.update({"finished_at": utc_timestamp(clock()), "exit_code": exit_code,
                       "state": "succeeded" if valid else "timed_out" if timed_out else "failed",
                       "category": category, "timed_out": timed_out})
    except Exception as error:
        record.update({"finished_at": utc_timestamp(clock()), "state": "failed", "exit_code": 1,
                       "category": "supervisor_child_" + type(error).__name__})
    atomic_json(path, record)
    # Retain every attempt's metadata even though raw evidence has a 72h cap.
    atomic_json(config.state_dir / "history" / f"{start}-{attempt}.json", record)
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Process at most one due window")
    parser.add_argument("--dry-run", action="store_true", help="Prepare one due window without launching an agent")
    options = parser.parse_args()
    os.umask(0o077)
    config = Config.from_environment()
    lock = acquire_lock(config.state_dir)
    # Keep the open descriptor referenced until main exits; closing releases flock.
    progress = load_progress(config)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    while True:
        now = time.time()
        prune_private_evidence(config, now)
        if recover_recorded_success(config, progress):
            continue
        window = next_window(progress, now)
        if window is None:
            atomic_json(config.state_dir / "status.json", {
                "state": "waiting", "at": utc_timestamp(now),
                "finished_watermark": utc_timestamp(progress["finished_watermark"]),
                "next_window_end": utc_timestamp(progress["finished_watermark"] + PERIOD_SECONDS)})
            if options.once or options.dry_run:
                return 0
            time.sleep(min(30, progress["finished_watermark"] + PERIOD_SECONDS - now))
            continue
        record = run_window(config, *window, dry_run=options.dry_run)
        atomic_json(config.state_dir / "status.json", record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        commit_success(config, progress, record)
        if options.once or options.dry_run:
            return 0 if record["state"] in {"succeeded", "dry_run"} else 1
        if record["state"] != "succeeded":
            # Keep the same closed window for retry; never silently skip it.
            time.sleep(config.retry_seconds)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AlreadyRunning:
        print('{"state":"already_running","exit_code":3}', file=sys.stderr)
        raise SystemExit(3)
    except KeyboardInterrupt:
        print('{"state":"stopped","exit_code":130}', file=sys.stderr)
        raise SystemExit(130)
