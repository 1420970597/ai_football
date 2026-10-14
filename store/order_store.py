"""Durable order claims shared by SQLite and PostgreSQL deployments.

The order store owns only short persistence operations.  In particular, a
claim transaction is committed before the venue network request starts; an
ambiguous provider response remains an ``unknown`` receipt and is never
silently replayed.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import quote

from store.sql_backend import (POSTGRES, SQLiteAdapter, SQLAdapter,
                               StorageBackendError, backend_from_url)
from store.postgres import connect as pg_connect, postgres_enabled

BUDGET_MAX_AGE_S = 30.0


def money_cents(value: Any) -> int:
    """CNY debit precision, never rounded up or treated as unknown zero."""
    try:
        amount = Decimal(str(value))
        if isinstance(value, bool) or not amount.is_finite() or amount < 0:
            raise ValueError('资金金额无效')
        return int((amount * 100).to_integral_value(rounding=ROUND_DOWN))
    except (InvalidOperation, ValueError, OverflowError) as exc:
        raise StorageBackendError('资金金额无效') from exc


def validated_budget(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """A complete native venue snapshot is required; no wallet fallback."""
    if (snapshot.get('source') != 'leyu_ybty' or snapshot.get('currency') != 'CNY'
            or snapshot.get('complete') is not True):
        raise StorageBackendError('体育资金与未结注单尚未完整核验')
    try:
        at = float(snapshot['observed_at'])
        if not 0 <= time.time() - at <= BUDGET_MAX_AGE_S:
            raise ValueError
        available = money_cents(snapshot['available'])
        rows = snapshot['unsettled']
        if not isinstance(rows, list):
            raise ValueError
        external: dict[str, dict[str, Any]] = {}
        for item in rows:
            no, mid = str(item.get('order_no') or ''), str(item.get('match_id') or '')
            stake = money_cents(item.get('amount'))
            if not no or not mid or stake <= 0:
                raise ValueError
            value = {'order_no': no, 'match_id': mid, 'cents': stake}
            if no in external and external[no] != value:
                raise ValueError
            external[no] = value
        return {'observed_at': at, 'available_cents': available,
                'external': list(external.values())}
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise StorageBackendError('资金快照缺失、过期或存在不一致注单') from exc


class OrderStore:
    """Backend-neutral storage for idempotent betting order receipts."""

    def __init__(self, adapter: SQLAdapter, *, path: Path | None = None) -> None:
        self.adapter = adapter
        self.path = path
        self.dialect = adapter.dialect
        self._lock = threading.RLock()
        self._closed = False

    @classmethod
    def from_config(cls, root: str | None, url: str | None = None) -> "OrderStore":
        configured = (url if url is not None else
                      os.environ.get("BETTING_DB_URL") or
                      os.environ.get("ORDER_DB_URL") or "").strip()
        if not configured and postgres_configured():
            configured = postgres_dsn_from_env()
        if configured:
            if configured.startswith(("postgres://", "postgresql://")):
                # backend_from_url deliberately does not fall back to a local
                # file when a PostgreSQL URL is configured.
                return cls(backend_from_url(configured), path=None)
            if configured.startswith("sqlite:///"):
                path = Path(configured[9:]).expanduser()
                return cls(backend_from_url(configured, sqlite_path=path), path=path)
            raise StorageBackendError("仅支持 sqlite:/// 或 postgresql:// 订单存储地址")
        if not root:
            raise StorageBackendError("未配置持久化订单目录")
        if postgres_enabled():
            # DATABASE_BACKEND=postgres is the compose-wide switch.  Build the
            # connection from the split PG_* variables so credentials stay in
            # env_file and no local betting-orders.sqlite3 is created.
            return cls(SQLAdapter(pg_connect(), POSTGRES), path=None)
        path = Path(root).parent / "betting-orders.sqlite3"
        return cls(backend_from_url(None, sqlite_path=path), path=path)

    @property
    def is_postgres(self) -> bool:
        return self.dialect.name == POSTGRES.name

    def initialize(self) -> None:
        """Create the schema and indexes, migrating only old SQLite files."""
        with self._lock:
            if self._closed:
                raise StorageBackendError("订单存储已关闭")
            if isinstance(self.adapter, SQLiteAdapter):
                self._initialize_sqlite()
            else:
                self._initialize_postgres()
            with self.adapter.transaction(write=True):
                self.adapter.execute('CREATE TABLE IF NOT EXISTS order_reservations ('
                    'identity TEXT PRIMARY KEY, account_key TEXT NOT NULL, match_id TEXT NOT NULL, '
                    'cents BIGINT NOT NULL, held INTEGER NOT NULL, order_no TEXT, '
                    'reconciled_at DOUBLE PRECISION, source TEXT)')
                self.adapter.execute('CREATE INDEX IF NOT EXISTS reservations_account '
                                     'ON order_reservations(account_key,held)')
                self.adapter.execute('CREATE TABLE IF NOT EXISTS betting_budget_snapshots ('
                    'account_key TEXT PRIMARY KEY, observed_at DOUBLE PRECISION NOT NULL, payload TEXT NOT NULL)')

    def _initialize_sqlite(self) -> None:
        assert self.path is not None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.adapter.transaction(write=True):
                self.adapter.execute("""CREATE TABLE IF NOT EXISTS orders (
                    identity TEXT PRIMARY KEY, at REAL NOT NULL, status TEXT NOT NULL,
                    payload TEXT NOT NULL, selection_key TEXT NOT NULL, account_key TEXT)""")
                columns = {row[1] for row in self.adapter.execute("PRAGMA table_info(orders)")}
                for name in ("selection_key", "account_key"):
                    if name not in columns:
                        self.adapter.execute("ALTER TABLE orders ADD COLUMN " + name + " TEXT")
                for old_identity, raw in self.adapter.execute(
                        "SELECT identity,payload FROM orders WHERE selection_key IS NULL").fetchall():
                    previous = json.loads(raw)
                    if not isinstance(previous, dict) or not all(
                            previous.get(key) for key in ("match_id", "market", "outcome")):
                        raise StorageBackendError("历史订单标识不完整，无法安全去重")
                    # Import locally to avoid coupling the store to the
                    # executor's module at import time.
                    from service.betting import _bet_selection, _identity
                    selection = _identity(_bet_selection(str(previous["match_id"]), previous))
                    self.adapter.execute("UPDATE orders SET selection_key=? WHERE identity=?",
                                         (selection, old_identity))
                self.adapter.execute(
                    "CREATE INDEX IF NOT EXISTS orders_selection ON orders(selection_key,account_key)")
            os.chmod(self.path, 0o600)
        except sqlite3.Error as exc:
            raise StorageBackendError("订单数据库初始化失败") from exc

    def _initialize_postgres(self) -> None:
        # This branch intentionally contains no SQLite PRAGMA or qmark/ALTER
        # migration statements.  PostgreSQL deployments start with this full
        # schema and can apply migrations independently before rollout.
        with self.adapter.transaction(write=True):
            self.adapter.execute("""CREATE TABLE IF NOT EXISTS orders (
                identity TEXT PRIMARY KEY,
                at DOUBLE PRECISION NOT NULL,
                status TEXT NOT NULL,
                payload TEXT NOT NULL,
                selection_key TEXT NOT NULL,
                account_key TEXT
            )""")
            self.adapter.execute(
                "CREATE INDEX IF NOT EXISTS orders_selection ON orders(selection_key,account_key)")
            self.adapter.execute(
                "CREATE INDEX IF NOT EXISTS orders_at ON orders(at DESC)")

    @staticmethod
    def _receipt(raw: Any) -> dict[str, Any]:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(str(raw))
        if not isinstance(value, dict):
            raise ValueError("订单回执格式无效")
        if value.get("status") == "sending":
            value.update(status="unknown", submitted=None,
                         reason="上次提交未取得回执；请核对场馆注单")
        return value

    def existing_order(self, selection_key: str, account_key: str,
                       *, for_update: bool = False) -> dict[str, Any] | None:
        with self._lock:
            sql = ("SELECT payload FROM orders WHERE selection_key=? "
                   "AND (account_key=? OR account_key IS NULL) "
                   "ORDER BY at DESC,identity LIMIT 1")
            if for_update and self.is_postgres:
                sql += " FOR UPDATE"
            row = self.adapter.execute(sql, (selection_key, account_key)).fetchone()
            return None if row is None else {**self._receipt(row[0]), "duplicate": True}

    def claim(self, identity: str, sending: Mapping[str, Any], selection_key: str,
              account_key: str, *, authorize: Callable[[Callable[[], Any]], Any] | None = None,
              on_duplicate: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
              budget: Mapping[str, Any] | None = None,
              total_fraction: float = .10, match_fraction: float = .02
              ) -> tuple[bool, dict[str, Any] | None]:
        """Atomically check and reserve an order before any provider call."""
        with self._lock:
            with self.adapter.transaction(write=True):
                self._account_lock(account_key)
                previous = self.existing_order(selection_key, account_key,
                                                for_update=self.is_postgres)
                if previous is not None:
                    return False, previous if on_duplicate is None else on_duplicate(previous)

                if budget is not None:
                    self._reserve(identity, sending, account_key, budget,
                                  total_fraction, match_fraction)

                insert_sql = self.dialect.insert_ignore(
                    "orders", ("identity", "at", "status", "payload", "selection_key", "account_key"))
                params = (identity, __import__("time").time(), "sending",
                          json.dumps(dict(sending), ensure_ascii=False), selection_key, account_key)
                if authorize is None:
                    cursor = self.adapter.execute(insert_sql, params)
                else:
                    cursor = authorize(lambda: self.adapter.execute(insert_sql, params))
                if getattr(cursor, "rowcount", 1) == 0:
                    self.adapter.execute('DELETE FROM order_reservations WHERE identity=?', (identity,))
                    previous = self.existing_order(selection_key, account_key,
                                                    for_update=self.is_postgres)
                    if previous is not None:
                        return False, previous
                    return False, {"status": "duplicate", "duplicate": True,
                                   "reason": "该推荐已有订单提交记录"}
                return True, None

    def update(self, identity: str, status: str, payload: Mapping[str, Any]) -> None:
        with self._lock:
            with self.adapter.transaction(write=True):
                account = self.adapter.execute('SELECT account_key FROM orders WHERE identity=?',
                                               (identity,)).fetchone()
                if account:
                    self._account_lock(str(account[0] or 'legacy-unattributed'))
                self.adapter.execute("UPDATE orders SET status=?,payload=? WHERE identity=?",
                                     (status, json.dumps(dict(payload), ensure_ascii=False), identity))
                self.adapter.execute('UPDATE order_reservations SET held=?,order_no=COALESCE(?,order_no) '
                    'WHERE identity=?', (0 if status == 'rejected' else 1, payload.get('order_no'), identity))

    def _account_lock(self, account_key: str) -> None:
        if self.is_postgres:
            self.adapter.execute('SELECT pg_advisory_xact_lock(hashtextextended(?,0))',
                                 ('football-bet-budget:' + account_key,)).fetchone()

    def _backfill_reservations(self, account_key: str) -> None:
        # Backfill old unresolved claims conservatively. Their absence from a
        # venue page never certifies settlement or releases an unknown debit.
        old = self.adapter.execute('SELECT o.identity,o.account_key,o.payload FROM orders o '
            "WHERE o.status IN ('sending','unknown','accepted','pending') "
            'AND (o.account_key=? OR o.account_key IS NULL) '
            'AND NOT EXISTS (SELECT 1 FROM order_reservations r WHERE r.identity=o.identity)',
            (account_key,)).fetchall()
        for old_id, old_account, raw in old:
            data = json.loads(str(raw))
            cents = money_cents(data.get('stake'))
            if cents <= 0 or not data.get('match_id') or not old_account:
                raise StorageBackendError('历史未结发送意图缺少资金或账号信息，需权威对账')
            self.adapter.execute(self.dialect.insert_ignore('order_reservations',
                ('identity','account_key','match_id','cents','held','order_no')),
                (old_id, old_account, str(data['match_id']), cents, 1, data.get('order_no')))

    def _reserve(self, identity: str, sending: Mapping[str, Any], account_key: str,
                 snapshot: Mapping[str, Any], total_fraction: float, match_fraction: float) -> None:
        budget = validated_budget(snapshot)
        for fraction in (total_fraction, match_fraction):
            if isinstance(fraction, bool) or not 0 <= float(fraction) <= 1:
                raise StorageBackendError('资金敞口比例无效')
        self._backfill_reservations(account_key)
        persisted = self.adapter.execute('SELECT observed_at,payload FROM betting_budget_snapshots '
                                         'WHERE account_key=?', (account_key,)).fetchone()
        if persisted and float(persisted[0]) > budget['observed_at']:
            budget = json.loads(str(persisted[1]))
        if not 0 <= time.time() - float(budget['observed_at']) <= BUDGET_MAX_AGE_S:
            raise StorageBackendError('资金快照已过期')
        self.adapter.execute('INSERT INTO betting_budget_snapshots(account_key,observed_at,payload) '
            'VALUES (?,?,?) ON CONFLICT(account_key) DO UPDATE SET '
            'observed_at=EXCLUDED.observed_at,payload=EXCLUDED.payload',
            (account_key, budget['observed_at'], json.dumps(budget)))
        external = {item['order_no']: item for item in budget['external']}
        mid = str(sending.get('match_id') or '')
        stake = money_cents(sending.get('stake'))
        if not mid or stake <= 0:
            raise StorageBackendError('预占订单缺少比赛或金额')
        equity = budget['available_cents'] + sum(item['cents'] for item in external.values())
        global_used = sum(item['cents'] for item in external.values())
        match_used = sum(item['cents'] for item in external.values() if item['match_id'] == mid)
        unseen = 0
        held = self.adapter.execute('SELECT match_id,cents,order_no FROM order_reservations '
                                    'WHERE account_key=? AND held=1', (account_key,)).fetchall()
        for match, cents, no in held:
            venue = external.get(no)
            if venue is not None:
                if venue['match_id'] != match or venue['cents'] != cents:
                    raise StorageBackendError('场馆注单与资金预占不一致')
                continue
            unseen += int(cents)
            global_used += int(cents)
            match_used += int(cents) if match == mid else 0
        global_cap = int(Decimal(str(total_fraction)) * equity)
        match_cap = int(Decimal(str(match_fraction)) * equity)
        if (stake > budget['available_cents'] - unseen or global_used + stake > global_cap
                or match_used + stake > match_cap):
            raise StorageBackendError('真实投注超过可用资金、全账户或单场未结敞口上限')
        self.adapter.execute(self.dialect.insert_ignore('order_reservations',
            ('identity','account_key','match_id','cents','held','order_no')),
            (identity, account_key, mid, stake, 1, None))

    def reconcile_settled(self, account_key: str, receipts: list[Mapping[str, Any]]) -> int:
        """Only exact native settled order numbers release held principal."""
        with self._lock, self.adapter.transaction(write=True):
            self._account_lock(account_key)
            self._backfill_reservations(account_key)
            count = 0
            for receipt in receipts:
                no = str(receipt.get('order_no') or '')
                if receipt.get('source') != 'leyu_ybty' or receipt.get('status') != 'settled' or not no:
                    continue
                cursor = self.adapter.execute('UPDATE order_reservations SET held=0,reconciled_at=?,source=? '
                    'WHERE account_key=? AND order_no=? AND held=1',
                    (time.time(), 'leyu_ybty_settled_receipt', account_key, no))
                count += max(0, getattr(cursor, 'rowcount', 0))
            return count

    def reservation_summary(self, account_key: str) -> dict[str, int]:
        """Aggregate diagnostics only; never expose native order identifiers."""
        with self._lock:
            row = self.adapter.execute(
                'SELECT count(*),COALESCE(sum(cents),0),'
                "COALESCE(sum(CASE WHEN order_no IS NULL OR order_no='' THEN 1 ELSE 0 END),0) "
                'FROM order_reservations WHERE account_key=? AND held=1',
                (account_key,)).fetchone()
            return {'held_count': int(row[0]), 'held_cents': int(row[1]),
                    'held_without_order_number': int(row[2])} if row else {
                        'held_count': 0, 'held_cents': 0, 'held_without_order_number': 0}

    def orders_for_selections(self, selection_keys: list[str], account_key: str
                              ) -> list[dict[str, Any]]:
        if not selection_keys:
            return []
        with self._lock:
            result: list[dict[str, Any]] = []
            for offset in range(0, len(selection_keys), 200):
                batch = selection_keys[offset:offset + 200]
                rows = self.adapter.execute(
                    "SELECT payload FROM orders WHERE selection_key IN (" +
                    ",".join(self.dialect.marker for _ in batch) +
                    ") AND (account_key=? OR account_key IS NULL) ORDER BY at",
                    (*batch, account_key)).fetchall()
                result.extend(self._receipt(row[0]) for row in rows)
            return result

    def recent(self, limit: int = 20) -> list[dict[str, Any]]:
        if limit < 0:
            raise ValueError("订单数量不能为负")
        with self._lock:
            rows = self.adapter.execute("SELECT payload FROM orders ORDER BY at DESC LIMIT ?",
                                        (int(limit),)).fetchall()
            return [self._receipt(row[0]) for row in rows]

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self.adapter.close()
                self._closed = True

    def __del__(self) -> None:  # pragma: no cover - interpreter cleanup
        try:
            self.close()
        except Exception:
            pass


__all__ = ["OrderStore"]


def postgres_configured() -> bool:
    """Whether the deployment explicitly selected the PostgreSQL backend."""
    backend = os.environ.get("DATABASE_BACKEND", "").strip().lower()
    if backend in {"sqlite", "local"}:
        return False
    return backend == "postgres" or all(os.environ.get(key, "").strip() for key in (
        "PG_BASE_URL", "PG_BASE_PORT", "PG_DATASET", "PG_USER", "PG_PASSWORD"))


def postgres_dsn_from_env() -> str:
    """Build a libpq DSN without logging or exposing the password."""
    required = ("PG_BASE_URL", "PG_BASE_PORT", "PG_DATASET", "PG_USER", "PG_PASSWORD")
    missing = [key for key in required if not os.environ.get(key, "").strip()]
    if missing:
        raise StorageBackendError("PostgreSQL 配置缺少：" + ",".join(missing))
    return ("postgresql://" + quote(os.environ["PG_USER"], safe="") + ":" +
            quote(os.environ["PG_PASSWORD"], safe="") + "@" +
            os.environ["PG_BASE_URL"] + ":" + os.environ["PG_BASE_PORT"] + "/" +
            quote(os.environ["PG_DATASET"], safe=""))


__all__ += ["postgres_configured", "postgres_dsn_from_env"]
