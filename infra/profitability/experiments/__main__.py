"""Run the audited experiments against one immutable set of offline evidence."""

import argparse
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path

from infra.profitability.experiments import inputs


PAPER_START = datetime.fromisoformat('2026-11-20T05:00:00Z')
PAPER_END = datetime.fromisoformat('2026-11-22T07:40:00Z')
EXPERT_SPLIT = datetime.fromisoformat('2026-11-18T00:00:00Z')
EXPERIMENTS = tuple(f'C{n:02}' for n in range(1, 7)) + tuple(f'P{n:02}' for n in range(1, 9))


def run(input_root, public_root):
    from infra.profitability.experiments import (
        attribution, clock, closing_line, expert_edge, market, models, pricing, risk,
        threestage, timing, uncertainty,
    )

    archive = inputs.Archive(input_root)
    native = archive.native()
    archive.market()
    frames = archive.frames()
    cohort = input_root / inputs.MARKET_FILE
    reference = input_root / 'reference' / 'reference-price.json'
    books = reference.parent / 'books'
    experiments = {
        'C01': clock.analyze(native, frames, PAPER_START, PAPER_END),
        'C02': pricing.analyze(frames, reference_root=reference.parent,
                               cost_root=input_root / 'cost-evidence', cutoff=PAPER_END),
        'C03': expert_edge.run(input_root, EXPERT_SPLIT),
        'C04': risk.capacity(native),
        'C05': risk.exposure(native),
        'C06': uncertainty.economics(native),
        'P01': attribution.analyze(frames, PAPER_START, PAPER_END),
        'P02': closing_line.run(input_root, public_root),
        'P03': market.run_calibration(cohort),
        'P04': models.run(public_root) | {
            'by_division': {division: models.run(public_root, division=division)
                            for division in ('E0', 'D1')},
        },
        'P05': threestage.analyze(native),
        'P06': uncertainty.resampling(native),
        'P07': market.run_contracts(reference, books, native_frames=frames,
                                    cost_root=input_root / 'cost-evidence', cutoff=PAPER_END),
        'P08': timing.run(cohort, reference, books) | {
            'native_entry_latency': clock.frozen_orders(native['rows'], inputs.utc(native['cutoff'])),
        },
    }
    manifest = archive.digests_document() | {
        'public': inputs.directory_digest(public_root, '*.csv'),
        'reference_manifest': inputs.file_digest(reference),
        'reference_books': inputs.directory_digest(books, '*.jsonl'),
        'cost_evidence': inputs.directory_digest(input_root / 'cost-evidence', '*'),
    }
    manifest_hash = sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    return {'study': 'profitability-experiments-20261122',
            'scope': 'offline_research', 'input_sha256': manifest_hash,
            'input_digests': manifest, 'experiments': experiments}


def encode(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f'Unsupported result type: {type(value).__name__}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input', type=Path, default=Path('/input'))
    parser.add_argument('--public', type=Path, default=Path('/public'))
    options = parser.parse_args()
    print(json.dumps(run(options.input, options.public), default=encode, sort_keys=True, allow_nan=False))


if __name__ == '__main__':
    main()
