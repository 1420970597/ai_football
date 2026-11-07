"""Set-based nickname updates for already trusted stable Telegram identities."""
from __future__ import annotations


def profile_updates(profiles) -> list[dict]:
    names = {}
    for profile in profiles:
        nickname = profile.nickname.strip()
        if not nickname or any(ord(char) < 32 for char in nickname):
            raise ValueError('invalid_profile_nickname')
        previous = names.setdefault(profile.sender_id, nickname)
        if previous != nickname:
            raise ValueError('conflicting_profile_nicknames')
    return [{'sender_id': sender, 'nickname': names[sender]} for sender in sorted(names)]


async def sync_trusted_profiles(conn, source_id, profiles: list[dict], observed_at) -> int:
    if not profiles:
        return 0
    # Caller holds the source lease and owns the directory transaction. Lock in
    # stable order across groups that trust the same expert; never grant trust.
    return await conn.fetchval(
        "WITH incoming AS MATERIALIZED ("
        " SELECT * FROM jsonb_to_recordset($2::jsonb) AS p(sender_id text,nickname text)),"
        " locked AS MATERIALIZED ("
        " SELECT e.id,e.display_nickname,p.nickname FROM v2_experts e "
        " JOIN v2_source_experts se ON se.expert_id=e.id "
        " JOIN incoming p ON p.sender_id=e.platform_sender_id "
        " WHERE se.source_id=$1 AND se.enabled AND e.enabled ORDER BY e.id FOR UPDATE OF e),"
        " aliases AS ("
        " INSERT INTO v2_expert_nickname_history(expert_id,nickname,first_seen_at,last_seen_at) "
        " SELECT id,nickname,$3,$3 FROM ("
        " SELECT id,display_nickname AS nickname FROM locked UNION SELECT id,nickname FROM locked) names "
        " ORDER BY id,nickname ON CONFLICT(expert_id,nickname) DO UPDATE "
        " SET last_seen_at=GREATEST(v2_expert_nickname_history.last_seen_at,EXCLUDED.last_seen_at)),"
        " updated AS ("
        " UPDATE v2_experts e SET display_nickname=l.nickname,updated_at=now() FROM locked l "
        " WHERE e.id=l.id AND e.display_nickname IS DISTINCT FROM l.nickname RETURNING e.id) "
        " SELECT count(*)::int FROM updated", source_id, profiles, observed_at)
