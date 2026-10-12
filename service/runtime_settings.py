"""Validated, versioned hot settings. Persistence contains write-only LLM keys."""
from __future__ import annotations

import json
import math
import os
import threading
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from .betting import betting_capability
from store.postgres import postgres_enabled
from store.runtime_settings_db import load_runtime_settings, save_runtime_settings

ALGORITHMS = {
    "poisson_market": "当前盘口 Poisson",
    "poisson_time_decay": "时间衰减 Poisson",
    "devig_consensus": "去水共识",
    "economics_risk_adjusted": "经济学风控",
    "microstructure_adjusted": "盘口微观结构",
}
DECISION_MODES = {'algorithm': '纯算法', 'model': '纯决策模型', 'hybrid': '算法 + 决策模型'}


class VersionConflict(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeConfig:
    algorithms: Tuple[str, ...] = ("poisson_market", "poisson_time_decay")
    primary_algorithm: str = "poisson_time_decay"
    devig_method: str = "proportional"
    min_probability: float = 0.52
    min_ev: float = 0.02
    quote_max_age_s: float = 15.0
    state_max_age_s: float = 90.0
    anchor_max_age_s: float = 120.0
    devig_spread_warn_pp: float = 1.0
    fractional_kelly: float = 0.25
    max_total_exposure: float = 0.25
    risk_correlation: float = 0.40
    execution_cost: float = 0.001
    weight_prior_matches: float = 20.0
    max_algorithm_weight: float = 0.60
    algorithm_alert_enabled: bool = True
    algorithm_alert_min_samples: int = 30
    algorithm_alert_threshold: float = 0.50
    # Opt-in for the background YBTY order executor; default remains off.
    betting_enabled: bool = False
    betting_stake_mode: str = "fixed"
    betting_fixed_stake: float = 10.0
    betting_min_hit_count: int = 3
    # Settled evidence is independent from repeated appearances of one quote.
    # Keep real-money execution fail-closed until the performance sample is
    # large enough to be meaningful.
    betting_min_evidence_matches: int = 30
    # Require a measurable, current-quote advantage before a real order.
    # This is deliberately stricter than the recommendation display gate.
    betting_min_edge: float = 0.05
    betting_retry_max_attempts: int = 5
    betting_retry_base_delay_s: float = 2.0
    llm_experiment_enabled: bool = True
    llm_enabled: bool = False
    llm_base_url: str = ""
    llm_model: str = ""
    llm_fallback_models: str = ""
    llm_api_key: str = field(default="", repr=False)
    llm_timeout_s: float = 20.0
    llm_temperature: float = 0.2
    llm_max_tokens: int = 2048
    llm_interval_s: float = 60.0
    llm_review_max_age_s: float = 120.0
    model_shadow_enabled: bool = True
    # Live inference route.  ``algorithm`` is the existing market ensemble,
    # ``model`` requires an approved active model, and ``hybrid`` blends both.
    decision_mode: str = "algorithm"
    model_blend_weight: float = 0.50
    model_training_enabled: bool = True
    model_training_matches: int = 100
    model_training_cpu: int = 1
    model_training_memory_mb: int = 2048


RANGES = {
    "model_training_matches": (1, 100000), "model_training_cpu": (1, 2),
    "model_training_memory_mb": (256, 4096),
    "weight_prior_matches": (2.0, 1000.0), "max_algorithm_weight": (0.2, 1.0),
    "min_probability": (0.0, 1.0), "min_ev": (0.0, 1.0),
    "quote_max_age_s": (1.0, 300.0), "state_max_age_s": (5.0, 600.0),
    "anchor_max_age_s": (5.0, 600.0), "llm_timeout_s": (1.0, 120.0),
    "devig_spread_warn_pp": (0.1, 20.0), "fractional_kelly": (0.0, 1.0),
    "max_total_exposure": (0.0, 1.0), "risk_correlation": (0.0, 0.99),
    "execution_cost": (0.0, 0.1),
    "llm_temperature": (0.0, 2.0), "llm_max_tokens": (256, 8192),
    "llm_interval_s": (10.0, 3600.0),
    "llm_review_max_age_s": (30.0, 600.0),
    "algorithm_alert_min_samples": (1, 100000),
    "algorithm_alert_threshold": (0.0, 1.0),
    "betting_fixed_stake": (0.01, 100000.0),
    "betting_min_hit_count": (1, 100000),
    "betting_min_evidence_matches": (1, 100000),
    "betting_min_edge": (0.0, 1.0),
    "betting_retry_max_attempts": (1, 20),
    "betting_retry_base_delay_s": (0.5, 30.0),
    "model_blend_weight": (0.0, 1.0),
}


def validated(base: RuntimeConfig, patch: Mapping[str, Any]) -> RuntimeConfig:
    unknown = set(patch) - set(base.__dataclass_fields__)
    if unknown:
        raise ValueError("未知配置字段")
    values = dict(patch)
    for key, (lo, hi) in RANGES.items():
        if key not in values:
            continue
        v = values[key]
        if key.startswith("model_training_") and not isinstance(v, int):
            raise ValueError(key + " 必须是整数")
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or not lo <= v <= hi:
            raise ValueError("%s 必须在 %s~%s 之间" % (key, lo, hi))
        if key == "llm_max_tokens" and (not isinstance(v, int)):
            raise ValueError("llm_max_tokens 必须是整数")
        if key in ("betting_min_hit_count", "betting_min_evidence_matches", "betting_retry_max_attempts") and not isinstance(v, int):
            raise ValueError(key + " 必须是整数")
    for name in ("llm_enabled", "llm_experiment_enabled", "algorithm_alert_enabled",
                 "betting_enabled", "model_training_enabled", "model_shadow_enabled"):
        if name in values and not isinstance(values[name], bool):
            raise ValueError(name + " 必须是布尔值")
    if "algorithms" in values:
        a = values["algorithms"]
        if not isinstance(a, (list, tuple)) or not a or any(not isinstance(x, str) or x not in ALGORITHMS for x in a):
            raise ValueError("至少启用一个有效算法")
        values["algorithms"] = tuple(dict.fromkeys(a))
    for key in ("primary_algorithm", "devig_method", "llm_base_url", "llm_model",
                "llm_fallback_models", "llm_api_key", "betting_stake_mode", "decision_mode"):
        if key in values:
            if not isinstance(values[key], str) or len(values[key]) > 4096:
                raise ValueError("%s 必须是有效字符串" % key)
            values[key] = values[key].strip()
    cfg = replace(base, **values)
    if cfg.primary_algorithm not in cfg.algorithms:
        raise ValueError("主算法必须属于已启用算法")
    if cfg.devig_method not in ("proportional", "power"):
        raise ValueError("去水方法必须是 proportional 或 power")
    if cfg.betting_stake_mode not in ("fixed", "confidence_multiplier"):
        raise ValueError("投注额度模式必须是 fixed 或 confidence_multiplier")
    if cfg.decision_mode not in ("algorithm", "model", "hybrid"):
        raise ValueError("decision_mode 必须是 algorithm、model 或 hybrid")
    if cfg.llm_base_url:
        u = urlsplit(cfg.llm_base_url)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ValueError("LLM 地址必须为无凭据的 http/https 地址")
    if cfg.llm_enabled and (not cfg.llm_base_url or not cfg.llm_model):
        raise ValueError("启用 LLM 需要地址和模型")
    return cfg


class RuntimeSettings:
    def __init__(self, config: Optional[RuntimeConfig] = None) -> None:
        self._lock = threading.RLock()
        self.config = config or RuntimeConfig()
        self.version = 1
        self.path: Optional[Path] = None
        self.restore_error = ""
        self.persistence_backend = 'memory'

    def snapshot(self) -> Tuple[RuntimeConfig, int]:
        with self._lock:
            return self.config, self.version

    def claim_bet(self, version: int, claim: Callable[[], None]) -> None:
        """Serialize a durable send intent with switch/version changes; no network here."""
        with self._lock:
            if version != self.version or not self.config.betting_enabled:
                raise VersionConflict("提交前投注配置已变化")
            claim()

    def bind(self, root: Optional[str]) -> None:
        with self._lock:
            self.path = Path(root).parent / "runtime-settings.json" if root else None
            self.persistence_backend = 'postgres' if self.path is not None and postgres_enabled() else ('local' if self.path else 'memory')
            restored = False
            seed_cfg, seed_version = self.config, self.version
            if self.path is not None and self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                    cfg = validated(self.config, raw["settings"])
                    v = raw["version"]
                    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                        raise ValueError("配置版本无效")
                    seed_cfg, seed_version = cfg, v
                    restored = True
                except (OSError, ValueError, KeyError, TypeError):
                    self.restore_error = "持久配置无效，已保留当前配置"
            # Startup activation only seeds a new installation. Explicitly
            # saved switches and parameters survive every subsequent restart.
            auto_enable = os.environ.get("AUTO_ENABLE_BETTING", "").strip().lower() in (
                "1", "true", "yes", "on")
            if auto_enable and not restored:
                seed_cfg = replace(seed_cfg, betting_enabled=True)
            if self.persistence_backend == 'postgres':
                try:
                    raw = load_runtime_settings({'version':seed_version,'settings':asdict(seed_cfg)})
                    if raw is None:
                        raise ValueError('PG 配置不存在')
                    cfg = validated(self.config, raw['settings'])
                    v = raw['version']
                    if isinstance(v,bool) or not isinstance(v,int) or v<1:
                        raise ValueError('PG 配置版本无效')
                except Exception as exc:
                    self.restore_error = 'PG 持久配置不可读取，未回退默认参数'
                    raise ValueError(self.restore_error) from exc
                self.config,self.version = cfg,v
                self.restore_error = ''
            else:
                self.config,self.version = seed_cfg,seed_version

    def public(self) -> Dict[str, Any]:
        with self._lock:
            cfg = asdict(self.config)
            cfg["has_llm_key"] = bool(cfg.pop("llm_api_key"))
            return {"version": self.version, "settings": cfg,
                    "algorithms": ALGORITHMS, "decision_modes": DECISION_MODES,
                    "persistent": self.path is not None,
                    "persistence_backend": self.persistence_backend,
                    "restore_error": self.restore_error,
                    "capabilities": {"betting": betting_capability(self.config)}}

    def update(self, patch: Mapping[str, Any], version: Any,
               apply: Callable[[RuntimeConfig, int], None]) -> Dict[str, Any]:
        with self._lock:
            if isinstance(version, bool) or not isinstance(version, int) or version != self.version:
                raise VersionConflict("配置已被其他页面更新，请重新读取")
            cfg = validated(self.config, patch)
            new_version = self.version + 1
            if self.persistence_backend == 'postgres':
                try:
                    changed = save_runtime_settings({'version':new_version,'settings':asdict(cfg)}, self.version)
                except Exception as exc:
                    raise ValueError('保存 PG 配置失败，当前配置未修改') from exc
                if not changed:
                    raw = load_runtime_settings()
                    if raw is not None:
                        latest = validated(self.config,raw['settings'])
                        apply(latest,raw['version'])
                        self.config,self.version = latest,raw['version']
                    raise VersionConflict('配置已被其他实例更新，请重新读取')
            elif self.path:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                try:
                    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "w", encoding="utf-8") as f:
                        json.dump({"version": new_version, "settings": asdict(cfg)}, f, ensure_ascii=False, allow_nan=False)
                        f.flush()
                        os.fsync(f.fileno())
                    os.replace(tmp, self.path)
                    os.chmod(self.path, 0o600)
                except OSError as exc:
                    raise ValueError("保存配置失败，当前配置未修改") from exc
            apply(cfg, new_version)
            self.config, self.version = cfg, new_version
            self.restore_error = ""
            return self.public()
