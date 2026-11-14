"""Stop redundant model continuations without approving unresolved identities."""
async def repeated_identity_failure(conn,job_id,attempt_id,issues):
    allowed={'unfinished_search_count','unfinished_searches','unbound_mention_count',
             'unbound_mentions','detail_limit','text_intent_unverified','source_total_review_gaps'}
    if set(issues)-allowed or issues.get('unfinished_search_count'):return False
    gaps=issues.get('source_total_review_gaps')or[]
    if gaps:
        # One explicit total still cannot be read before its unique fixture
        # exists. Only a complete negative original-pair/league search may
        # terminate that gap, always unverified. Multiple items or candidate
        # mentions must continue; they may refer to an independent fixture.
        if (len(gaps)!=1 or issues.get('unbound_mention_count')
                or set(gaps[0])!={'line','period','selection','source_text'}
                or gaps[0].get('period')not in {'full_time','first_half','second_half'}
                or gaps[0].get('selection')not in {'over','under'}):return False
        if not await conn.fetchval(
            "WITH pairs AS (SELECT DISTINCT payload->'parameters'->>'team_a' a,"
            "payload->'parameters'->>'team_b' b FROM v2_analysis_traces "
            "WHERE job_id=$1 AND payload->>'attempt_id'=($2::uuid)::text "
            "AND trace_kind='mcp_search_matches' AND payload->>'status'='succeeded' "
            "AND length(payload->'parameters'->>'team_a')>=2 "
            "AND length(payload->'parameters'->>'team_b')>=2) "
            "SELECT (SELECT count(*) FROM pairs)=1 "
            "AND NOT EXISTS(SELECT 1 FROM v2_review_fixture_bindings WHERE job_id=$1 AND attempt_id=$2) "
            "AND NOT EXISTS(SELECT 1 FROM v2_match_searches WHERE job_id=$1 AND attempt_id=$2 AND NOT complete)",
            job_id,attempt_id):return False
    if not issues.get('unbound_mention_count'):
        if not issues.get('text_intent_unverified'):return False
        # A completed whole-league source search plus a failed original-pair
        # search is an explainable unresolved result, never identity approval.
        league_complete=bool(await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM v2_analysis_traces league "
            "WHERE league.job_id=$1 AND league.payload->>'attempt_id'=($2::uuid)::text "
            "AND league.trace_kind='mcp_search_matches' AND league.payload->>'status'='succeeded' "
            "AND league.payload->>'complete'='true' AND league.payload->'parameters'->>'team_query'='' "
            "AND length(league.payload->'parameters'->>'competition_query')>=2 "
            "AND league.payload->'parameters'->>'sport' IS NOT NULL "
            "AND league.payload->'parameters'->>'from_utc' IS NOT NULL "
            "AND league.payload->'parameters'->>'to_utc' IS NOT NULL "
            "AND league.payload->'resolution'->>'resolved'='false' "
            "AND league.payload->'resolution'->>'candidate_count'='0' "
            "AND EXISTS(SELECT 1 FROM v2_analysis_traces original WHERE original.job_id=league.job_id "
            "AND original.payload->>'attempt_id'=league.payload->>'attempt_id' "
            "AND original.trace_kind='mcp_search_matches' AND original.payload->>'status'='succeeded' "
            "AND original.payload->>'complete'='true' "
            "AND length(original.payload->'parameters'->>'team_query')>=2 "
            "AND original.payload->'resolution'->>'resolved'='false' "
            "AND original.payload->'resolution'->>'candidate_count'='0' "
            "AND ((original.payload->'parameters')-'team_query'-'competition_query'-'cursor'-'page_size')="
            "((league.payload->'parameters')-'team_query'-'competition_query'-'cursor'-'page_size')))",
            job_id,attempt_id))
        if league_complete:return True
        if gaps:return False
        # Sometimes the model correctly reports an unverified original pair
        # without naming a candidate, so there is no unbound mention. A second
        # explicit refusal for that same completed search is still evidence.
        # This only ends unverified; it never creates binding or acceptance.
        return bool(await conn.fetchval(
            "WITH calls AS (SELECT trace_kind,payload FROM v2_analysis_traces "
            "WHERE job_id=$1 AND payload->>'attempt_id'=($2::uuid)::text "
            "AND payload->>'status'='succeeded' "
            "AND trace_kind IN ('mcp_search_matches','mcp_resolve_match')), "
            "pairs AS (SELECT DISTINCT payload->'parameters'->>'team_a' a,"
            "payload->'parameters'->>'team_b' b FROM calls "
            "WHERE payload->'parameters'->>'team_a' IS NOT NULL "
            "AND payload->'parameters'->>'team_b' IS NOT NULL) "
            "SELECT (SELECT count(*) FROM pairs)=1 "
            "AND NOT EXISTS(SELECT 1 FROM v2_review_fixture_bindings WHERE job_id=$1 AND attempt_id=$2) "
            "AND NOT EXISTS(SELECT 1 FROM v2_match_searches WHERE job_id=$1 AND attempt_id=$2 AND NOT complete) "
            "AND EXISTS(SELECT 1 FROM calls s JOIN calls r ON "
            "r.payload->'parameters'->>'search_key'=s.payload->>'search_key' "
            "AND r.payload->'parameters'->>'team_a'=s.payload->'parameters'->>'team_a' "
            "AND r.payload->'parameters'->>'team_b'=s.payload->'parameters'->>'team_b' "
            "WHERE s.trace_kind='mcp_search_matches' AND s.payload->>'complete'='true' "
            "AND s.payload->'resolution'->>'resolved'='false' "
            "AND s.payload->'resolution'->>'candidate_count'='0' "
            "AND r.trace_kind='mcp_resolve_match' AND r.payload->'result'->>'resolved'='false' "
            "AND r.payload->'result'->>'candidate_count'='0')",job_id,attempt_id))
    mentions=issues.get('unbound_mentions')or[]
    if len(mentions)!=1 or issues['unbound_mention_count']!=1:return False
    # Parameters are system-captured tool arguments. A repeated negative
    # result cannot grant a binding, signal, or completion_verified status.
    keys={m.get('candidate',{}).get('search_key')for m in mentions if m.get('candidate')}
    if (not keys or None in keys or any(not m.get('candidate')
            or not m['candidate'].get('search_complete')for m in mentions)):
        return False
    # search_matches also performs a real resolve on its completed snapshot.
    # Count that system-captured negative result for the same pair and scope;
    # otherwise one automatic + one explicit refusal causes a third model turn.
    rows=await conn.fetch(
        "WITH negative AS (SELECT payload->'parameters' AS parameters,"
        "CASE WHEN trace_kind='mcp_search_matches' THEN payload->>'search_key' "
        "ELSE payload->'parameters'->>'search_key' END AS search_key "
        "FROM v2_analysis_traces WHERE job_id=$1 AND payload->>'attempt_id'=($2::uuid)::text "
        "AND payload->>'status'='succeeded' AND ("
        "(trace_kind='mcp_resolve_match' AND payload->'result'->>'resolved'='false' "
        "AND payload->'result'->>'candidate_count'='0') OR "
        "(trace_kind='mcp_search_matches' AND payload->>'complete'='true' "
        "AND payload->'resolution'->>'resolved'='false' "
        "AND payload->'resolution'->>'candidate_count'='0'))) "
        "SELECT search_key FROM negative WHERE search_key IS NOT NULL "
        "AND parameters->>'team_a' IS NOT NULL AND parameters->>'team_b' IS NOT NULL "
        "GROUP BY search_key,parameters->>'team_a',parameters->>'team_b' HAVING count(*)>=2",
        job_id,attempt_id)
    return keys<={r['search_key']for r in rows}
