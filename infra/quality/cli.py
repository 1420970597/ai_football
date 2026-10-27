"""Offline JSON quality diagnostics; all filesystem access remains in project."""

import argparse
import json
import os
from pathlib import Path

from .evaluate import compare, evaluate
from .formats import QualityError

ROOT = Path(__file__).resolve().parents[2]
MAX_INPUT_BYTES = 16 * 1024 * 1024


def project_path(value):
    path = Path(value).resolve()
    if not path.is_relative_to(ROOT):
        raise QualityError('only_project_paths_allowed')
    return path


def read(value):
    path = project_path(value)
    if path.stat().st_size > MAX_INPUT_BYTES:
        raise QualityError('input_size_budget_exceeded')
    try:
        return json.loads(path.read_text(encoding='utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError):
        raise QualityError('invalid_json_input') from None


def run(args):
    labels = read(args.labels)
    if args.mode == 'evaluate':
        report = evaluate(labels, read(args.run))
    else:
        report = compare(labels, read(args.baseline), read(args.candidate))
    if args.output:
        path = project_path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            json.dump(report, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write('\n')
        return {'report': str(path.relative_to(ROOT)), 'synthetic_only': True, 'mode': args.mode}
    return report


def main():
    parser = argparse.ArgumentParser(description='仅离线合成样本质量诊断，不调用模型、采集或投注')
    parser.add_argument('mode', choices=('evaluate', 'compare'))
    parser.add_argument('--labels', required=True)
    parser.add_argument('--run')
    parser.add_argument('--baseline')
    parser.add_argument('--candidate')
    parser.add_argument('--output')
    args = parser.parse_args()
    if args.mode == 'evaluate' and not args.run or args.mode == 'compare' and not (args.baseline and args.candidate):
        parser.error('evaluate需要--run；compare需要--baseline和--candidate')
    try:
        print(json.dumps(run(args), ensure_ascii=False, sort_keys=True))
    except Exception as exc:
        print(json.dumps({'error': str(exc) if isinstance(exc, QualityError) else type(exc).__name__}))
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
