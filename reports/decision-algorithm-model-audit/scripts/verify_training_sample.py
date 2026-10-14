#!/usr/bin/env python3
"""Read-only PostgreSQL contract/time sampling probe; never prints a DSN."""
from __future__ import annotations

import json
import argparse
import time
from collections import defaultdict
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from core.decision_features import FEATURE_VERSION
from core.learned_model import DATA_VERSION
from core.live_model import MODEL_VERSION
from store.training_data import _postgres_training_sample_query, dataset
from store.training_projection import projection_target_from_env, readonly_snapshot

CAP = 40
TIME_FILTER = ('cutoff_ms>0 AND decision_received_at_ms>0 AND quote_received_at_ms>0 '
               'AND decision_received_at_ms<=cutoff_ms AND quote_received_at_ms<=cutoff_ms')
VERSIONS = (DATA_VERSION, MODEL_VERSION, FEATURE_VERSION)


def inspect(cursor, query, args):
    started = time.monotonic()
    cursor.execute(query, args)
    names = [column[0] for column in cursor.description]
    returned = [dict(zip(names, values)) for values in cursor.fetchall()]
    rows = [row for row in returned if row.get('match_id') is not None]
    groups = defaultdict(set)
    for row in rows:
        groups[tuple(row[k] for k in ('match_id','at','algorithm','market','line'))].add(row['outcome'])
    expected = {'HAD': {'home','draw','away'}, 'OU': {'over','under'}, 'AH': {'home','away'}}
    incomplete = sum(outcomes != expected[str(key[3]).split('_1H')[0]] for key, outcomes in groups.items())
    identities = [tuple(row[k] for k in ('match_id','at','algorithm','market','line','outcome')) for row in rows]
    return {'rows':len(rows), 'time_points':len({r['at'] for r in rows}),
            'first_at':min((r['at'] for r in rows),default=None),
            'last_at':max((r['at'] for r in rows),default=None),
            'groups':len(groups), 'incomplete_groups':incomplete,
            'diagnostic_rows':len(returned)-len(rows),
            'curves':len({tuple(r[k] for k in ('match_id','at','algorithm','market')) for r in rows}),
            'duplicate_rows':len(identities)-len(set(identities)),
            'seconds':round(time.monotonic()-started,4)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--full-dataset',action='store_true')
    options = parser.parse_args()
    target = projection_target_from_env()
    if target is None or target.backend != 'postgresql':
        raise ValueError('需要配置 PostgreSQL 只读投影')
    results = []
    with readonly_snapshot(target) as con:
        cursor = con.cursor()
        cursor.execute("SELECT set_config('statement_timeout','20000',true)")
        # CTE fixtures run in the same PostgreSQL dialect; no tables or source
        # data are created/updated, and only pre-result identities vary.
        for points,market,invalid_clock in ((1,'HAD',False),(2,'HAD',False),(100,'HAD',False),
                                             (100,'OU',False),(100,'OU',True)):
            labels = "(SELECT 'fixture'::text match_id,'2025-01-02T00:00:00+00:00'::text available_at)"
            query, args = _postgres_training_sample_query(labels,TIME_FILTER,VERSIONS,1 if market=='OU' else CAP)
            contracts = ("(VALUES (''::text,'home'::text),('','draw'),('','away')) o(line,outcome)"
                         if market=='HAD' else "(VALUES ('2.5'::text,'over'::text),('2.5','under'),"
                         "('3.5','over'),('3.5','under')) o(line,outcome)")
            quote_clock = ("CASE WHEN line='3.5' AND outcome='under' THEN 1001.0 ELSE 1000.0 END"
                           if invalid_clock else '1000.0')
            prefix = ("WITH fixture_inputs AS MATERIALIZED (SELECT 'fixture'::text match_id,"
                "to_char('2025-01-01T12:00:00+00:00'::timestamptz+s*interval '1 second',"
                "'YYYY-MM-DD\"T\"HH24:MI:SS.US') || '+00:00' AS \"at\","
                "'poisson_market'::text algorithm,'"+market+"'::text market,line,outcome,"
                "1000.0::float8 cutoff_ms,1000.0::float8 decision_received_at_ms,"
                +quote_clock+"::float8 quote_received_at_ms,%s::text data_version,"
                "%s::text model_version,%s::text feature_version "
                "FROM generate_series(0,"+str(points-1)+") s CROSS JOIN "
                +contracts+") ")
            query = query.replace('model_inputs','fixture_inputs').removeprefix('WITH ')
            measured = inspect(cursor,prefix.rstrip()+', '+query,(*VERSIONS,*args))
            assert measured['rows']==0 if invalid_clock else measured['rows']>0,measured
            assert measured['incomplete_groups']==0 and measured['duplicate_rows']==0,measured
            assert measured['time_points'] == (0 if invalid_clock else min(points,1 if market=='OU' else 8)),measured
            if market=='OU' and not invalid_clock:
                assert measured['rows']==4 and measured['groups']==2,measured
            results.append({'scope':'sql_fixture','input_time_points':points,'market':market,
                            'invalid_clock':invalid_clock,**measured})
        cursor.execute("SELECT DISTINCT match_id,available_at FROM (SELECT match_id,available_at "
            "FROM model_labels UNION ALL SELECT match_id,available_at FROM label_revisions) labels "
            "ORDER BY available_at DESC LIMIT 8")
        mids = ['5519689',*[str(row[0]) for row in cursor.fetchall()]]
        for mid in dict.fromkeys(mids):
            labels = ("(SELECT match_id,MAX(available_at) available_at FROM (SELECT match_id,available_at "
                "FROM model_labels WHERE match_id=%s UNION ALL SELECT match_id,available_at "
                "FROM label_revisions WHERE match_id=%s) labels GROUP BY match_id)")
            query,args = _postgres_training_sample_query(labels,TIME_FILTER,VERSIONS,CAP)
            measured = inspect(cursor,query,(mid,mid,*args))
            assert measured['duplicate_rows']==0,measured
            results.append({'scope':'production_readonly','match_id':mid,**measured})
        cursor.close()
    summary = {'cap_target':CAP,'max_sql_rows':None,'sampling_unit':'complete market curves','results':results}
    if options.full_dataset:
        started = time.monotonic()
        audit = {}
        rows = dataset(Path('output/ledger.sqlite3'),audit=audit,max_per_match=CAP)
        summary['full_dataset'] = {'seconds':round(time.monotonic()-started,4),'rows':len(rows),
                                  'matches':len({r['match_id'] for r in rows}),'audit':audit}
    print(json.dumps(summary,ensure_ascii=False,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
