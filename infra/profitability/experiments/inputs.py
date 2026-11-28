"""Read-only loaders and digests for the frozen profitability archives.

The loader never writes, never follows a path outside the mounted input root
and never returns raw stakeholder identifiers to the caller's output; studies
reduce them to counts before they are serialized.
"""

from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from pathlib import Path

NATIVE_FILE = 'native-cohort.json'
MARKET_FILE = 'market-adjusted-cohort.json'
FRAMES_DIR = 'paper-preflight'
ONE_MINUTE = timedelta(minutes=1)


def utc(value):
    if isinstance(value, datetime):
        moment = value
    else:
        moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    if moment.utcoffset() != timedelta(0):
        raise ValueError('expected UTC timestamp')
    return moment


def number(value):
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def file_digest(path):
    return sha256(Path(path).read_bytes()).hexdigest()


def frames_digest(frames):
    """Digest a frame list exactly as the frozen paper pipeline does."""
    return sha256(json.dumps(frames, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def directory_digest(root, pattern='*.json'):
    """Aggregate digest over sorted file names and their bytes."""
    aggregate = sha256()
    for path in sorted(Path(root).glob(pattern)):
        aggregate.update(path.name.encode())
        aggregate.update(sha256(path.read_bytes()).digest())
    return aggregate.hexdigest()


def frame_clock(frame):
    """Validate one minute-archive frame and list boundary problems.

    The frozen capture protocol fixes: begin = slot - 90s, end = slot - 30s and
    observation inside [slot, slot + 30s). Problems are reported, not raised,
    so an archive defect becomes an explicit coverage failure.
    """
    try:
        slot, begin, end, observed = (utc(frame[key]) for key in ('slot', 'begin', 'end', 'observed_at'))
    except (KeyError, TypeError, ValueError):
        return {'problems': ['missing_or_invalid_timepoint']}
    problems = []
    if (slot.second or slot.microsecond or begin != slot - timedelta(seconds=90)
            or end != slot - timedelta(seconds=30)):
        problems.append('slot_boundary_mismatch')
    if not slot <= observed < slot + timedelta(seconds=30):
        problems.append('observation_outside_slot')
    return {'slot': slot, 'begin': begin, 'end': end, 'observed': observed, 'problems': problems}


class Archive:
    """A mounted archive read strictly inside ``root``."""

    def __init__(self, root):
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise ValueError('input root is not a directory')
        self.digests = {}

    def _path(self, relative):
        path = (self.root / relative).resolve()
        if self.root not in path.parents and path != self.root:
            raise ValueError('input path escapes the mounted root')
        return path

    def document(self, name):
        path = self._path(name)
        raw = path.read_bytes()
        self.digests[name] = sha256(raw).hexdigest()
        return json.loads(raw)

    def native(self):
        return self.document(NATIVE_FILE)

    def market(self):
        return self.document(MARKET_FILE)

    def frames(self):
        directory = self._path(FRAMES_DIR)
        frames = []
        for path in sorted(directory.glob('*.json')):
            frames.append(json.loads(path.read_bytes()))
        self.digests[FRAMES_DIR] = directory_digest(directory)
        return frames

    def digests_document(self):
        return dict(sorted(self.digests.items()))


def rows(document):
    value = document.get('rows') if isinstance(document, dict) else None
    if not isinstance(value, list):
        raise ValueError('archive document requires a rows array')
    return value


def interval(document):
    try:
        begin, end, cutoff = (utc(document[key]) for key in ('begin', 'end', 'cutoff'))
    except (KeyError, TypeError, ValueError):
        raise ValueError('archive document requires begin, end and cutoff UTC times')
    if not begin < end <= cutoff:
        raise ValueError('archive interval must be ordered and settled by cutoff')
    return begin, end, cutoff
