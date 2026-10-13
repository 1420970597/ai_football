/**
 * sentinel 校验流水线配置。
 *
 * 为什么必须显式写死命令：sentinel 的自动探测会生成裸 `mypy .` / `ruff check .`，
 * 而本仓库的 mypy/ruff 不在 PATH（实测 exit 127: mypy: not found）。
 * 本机可用的二进制在 /root/.pi-lens/pip-tools/bin/（mypy 2.4.0 / ruff 0.16.8），
 * 且 AGENTS.md §6 要求所有批处理经 scripts/cpu-limited.sh 限制 CPU（本机同跑 LLM 网关）。
 * 所以这里把三步都显式声明为 repo 约定的调用形式。
 */
export default {
  checks: {
    afterEdit: [
      {
        name: "mypy",
        cmd: "./scripts/cpu-limited.sh run -- /root/.pi-lens/pip-tools/bin/mypy",
        files: ["core/**/*.py", "store/**/*.py", "collector/**/*.py", "api/**/*.py", "tests/**/*.py"],
        timeoutMs: 240_000,
      },
    ],
    beforeDone: [
      {
        name: "mypy",
        cmd: "./scripts/cpu-limited.sh run -- /root/.pi-lens/pip-tools/bin/mypy",
        files: ["core/**/*.py", "store/**/*.py", "collector/**/*.py", "api/**/*.py", "tests/**/*.py"],
        timeoutMs: 240_000,
      },
      {
        name: "ruff",
        cmd: "./scripts/cpu-limited.sh run -- /root/.pi-lens/pip-tools/bin/ruff check .",
        files: ["**/*.py"],
        timeoutMs: 120_000,
        warnOnly: true,
      },
      {
        name: "test",
        cmd: "./scripts/cpu-limited.sh run -- python3 -m unittest discover -s tests -q",
        files: ["**/*.py"],
        timeoutMs: 600_000,
      },
    ],
  },
  repair: { enabled: true, maxAttempts: 3 },
  checkpoints: { enabled: true, retention: 30 },
  maxOutputTokens: 2500,
  maxTraceLines: 20,
  exclude: ["**/node_modules/**", "**/.git/**", "dist/**", "build/**", "coverage/**", "graph-out/**", "output/**", "reports/**/*.html", "**/.mypy_cache/**"],
  mindplace: true,
  contract: true,
};
