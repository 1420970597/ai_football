#!/usr/bin/env python3
"""Validate saved per-paper audit decisions; never count search hits as papers."""
from __future__ import annotations

import csv
from collections import Counter
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / 'data'
MIN_PAPERS = 50
TRUE_VALUES = {'yes', 'true', '1'}
REQUIRED_FIELDS = ('title', 'method', 'code_mapping', 'applicability',
                   'evidence_boundary', 'source_check', 'review_depth')


def included(row: dict[str, str]) -> bool:
    return row.get('included', '').lower() in TRUE_VALUES


def identity(row: dict[str, str]) -> str:
    doi = row.get('doi', '').lower().removeprefix('https://doi.org/').strip()
    return doi or re.sub(r'[^a-z0-9]', '', row['title'].lower())


def verify() -> dict:
    summary = {}
    chart_rows = []
    for track in ('algorithm', 'model'):
        with (DATA / (track + '_paper_audit.csv')).open(newline='', encoding='utf-8') as stream:
            rows = list(csv.DictReader(stream))
        selected = [row for row in rows if included(row)]
        if len(selected) < MIN_PAPERS:
            raise ValueError(f'{track}: only {len(selected)} included papers')
        keys = [identity(row) for row in selected]
        if len(set(keys)) != len(keys):
            raise ValueError(f'{track}: duplicate included DOI/title')
        for row in selected:
            if row.get('independent', 'true').lower() not in TRUE_VALUES:
                raise ValueError(f'{track}: non-independent included paper: {row["title"]}')
            missing = [name for name in REQUIRED_FIELDS if not row.get(name, '').strip()]
            if missing:
                raise ValueError(f'{track}: {row["title"]}: missing {missing}')
        depths = dict(Counter(row['review_depth'] for row in selected))
        topics = dict(Counter(row.get('topic', '') for row in selected))
        summary[track] = {'audited_records': len(rows), 'included_independent': len(selected),
                          'not_included_records': len(rows) - len(selected),
                          'review_depth': depths, 'topics': topics,
                          'count_basis': 'manual inclusion and independent-study audit; not retrieval hits'}
        chart_rows.extend({'track': track, 'category': category, 'papers': value}
                          for category, value in [('included', len(selected)),
                                                   ('not_included', len(rows) - len(selected))])
    with (DATA / 'audit_summary.csv').open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=('track', 'category', 'papers'), lineterminator='\n')
        writer.writeheader()
        writer.writerows(chart_rows)
    (DATA / 'audit_manifest.json').write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return summary


if __name__ == '__main__':
    print(json.dumps(verify(), ensure_ascii=False, indent=2))
