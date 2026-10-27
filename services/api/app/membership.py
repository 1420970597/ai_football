"""按当前有效能力合并权益；免费长期记录不会遮盖付费方案。"""

from datetime import datetime, timezone
from decimal import Decimal

from . import database


def resolve_memberships(rows, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    valid = [dict(row) for row in rows if row.get("status", "active") == "active"
             and row.get("enabled", True)
             and row.get("starts_at") is not None and row["starts_at"] <= now
             and (row.get("ends_at") is None or row["ends_at"] > now)]
    if not valid:
        return None
    # 主展示方案按价格和有效能力确定，权限本身由全部有效授予作 OR 合并。
    valid.sort(key=lambda row: (row["code"] != "free", Decimal(str(row.get("price", 0))),
                               sum(value is True for value in row.get("entitlements", {}).values()),
                               row["starts_at"], row["code"]), reverse=True)
    primary = valid[0]
    entitlements: dict[str, bool] = {}
    expiries: dict[str, datetime | None] = {}
    for row in valid:
        for key, enabled in row.get("entitlements", {}).items():
            if enabled is not True:
                continue
            entitlements[key] = True
            end = row.get("ends_at")
            if key not in expiries or end is None or (expiries[key] is not None and end > expiries[key]):
                expiries[key] = end
    return {
        "code": primary["code"], "name": primary["name"], "ends_at": primary["ends_at"],
        "entitlements": entitlements, "entitlement_expires_at": expiries,
        "active_plans": [{"id": str(row["id"]), "code": row["code"], "name": row["name"],
                          "starts_at": row["starts_at"], "ends_at": row["ends_at"]} for row in valid],
    }


async def get_membership(user_id, connection=None) -> dict | None:
    connection = connection or database.current_pool()
    rows = await connection.fetch(
        "SELECT m.id,m.status,m.starts_at,m.ends_at,p.code,p.name,p.price,p.entitlements,p.enabled "
        "FROM v2_memberships m JOIN v2_membership_plans p ON p.id=m.plan_id "
        "WHERE m.user_id=$1 AND m.status='active' AND p.enabled "
        "AND m.starts_at<=now() AND (m.ends_at IS NULL OR m.ends_at>now())", user_id,
    )
    return resolve_memberships(rows)


async def has_entitlement(user_id, key: str, connection=None) -> bool:
    membership = await get_membership(user_id, connection)
    return bool(membership and membership["entitlements"].get(key, False))
