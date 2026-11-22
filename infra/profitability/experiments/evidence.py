"""Provenance and parsing helpers shared by the isolated profitability experiments.

The experiments read frozen archives and public CSV files only. Nothing here opens a
socket, touches the business database, or writes to an input directory. Every section
keeps the digest of the bytes it consumed so a rerun can be compared, and every
section states an evidence label together with the reasons it could not measure
something.
"""

from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

MEASURED = 'measured'
INSUFFICIENT = 'insufficient_evidence'

PRICE_QUANTUM = Decimal('0.000001')


def utc(value):
    """Parse an ISO timestamp and require an explicit UTC offset."""
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).strip().replace('Z', '+00:00'))
    if moment.tzinfo is None or moment.utcoffset() != timedelta(0):
        raise ValueError(f'expected UTC timestamp, got {value!r}')
    return moment


def read_bytes(path):
    """Read an input file, keeping the failure reason instead of a bare traceback."""
    try:
        return Path(path).read_bytes()
    except OSError as error:
        raise FileNotFoundError(f'cannot read frozen input {path}: {error}') from error


def decimal(value, *, name='value'):
    """Parse a finite Decimal from a CSV cell or JSON string."""
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (ArithmeticError, InvalidOperation, ValueError) as error:
        raise ValueError(f'{name} is not a decimal: {value!r}') from error
    if not result.is_finite():
        raise ValueError(f'{name} is not finite: {value!r}')
    return result


def quantized(value):
    return Decimal(value).quantize(PRICE_QUANTUM)


def file_record(path, root=None):
    """Return a provenance record for one input file."""
    path = Path(path)
    raw = read_bytes(path)
    label = str(path.relative_to(root)) if root else str(path)
    return {'path': label, 'bytes': len(raw), 'sha256': sha256(raw).hexdigest()}


def load_json(path, root=None):
    """Load a frozen JSON archive and return the document with its digest."""
    path = Path(path)
    raw = read_bytes(path)
    record = file_record(path, root)
    return json.loads(raw), record


def counts(counter):
    """Stable JSON view of a Counter whose keys may be tuples."""
    return {str(key): value for key, value in sorted(counter.items(), key=lambda item: str(item[0]))}


def section(*, label, sources, missing=(), coverage=None, results=None, limitations=()):
    """One experiment result: label, inputs, measured coverage and gaps."""
    document = {
        'evidence_label': label,
        'sources': list(sources),
        'missing_reasons': sorted(set(missing)),
    }
    if limitations:
        document['limitations'] = sorted(set(limitations))
    if coverage is not None:
        document['coverage'] = coverage if not isinstance(coverage, Counter) else counts(coverage)
    if results is not None:
        document['results'] = results
    return document
