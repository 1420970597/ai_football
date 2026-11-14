"""Stop redundant model continuations without approving unresolved identities."""
async def repeated_identity_failure(conn,job_id,attempt_id,issues):
    allowed={'unfinished_search_count','unfinished_searches','unbound_mention_count',
             'unbound_mentions','detail_limit','text_intent_unverified'}
    if (set(issues)-allowed or issues.get('unfinished_search_count')
            or not issues.get('unbound_mention_count')):return False
    mentions=issues.get('unbound_mentions')or[]
    if len(mentions)!=1 or issues['unbound_mention_count']!=1:return False
    # Parameters are system-captured tool arguments. A repeated negative
    # result cannot grant a binding, signal, or completion_verified status.
    keys={m.get('candidate',{}).get('search_key')for m in mentions if m.get('candidate')}
    if (not keys or None in keys or any(not m.get('candidate')
            or not m['candidate'].get('search_complete')for m in mentions)):
        return False
    rows=await conn.fetch(
        "SELECT payload->'parameters'->>'search_key' AS search_key "
        "FROM v2_analysis_traces WHERE job_id=$1 AND payload->>'attempt_id'=($2::uuid)::text "
        "AND trace_kind='mcp_resolve_match' AND payload->>'status'='succeeded' "
        "AND payload->'result'->>'resolved'='false' "
        "AND payload->'result'->>'candidate_count'='0' "
        "GROUP BY payload->'parameters' HAVING count(*)>=2",job_id,attempt_id)
    return keys<={r['search_key']for r in rows}
