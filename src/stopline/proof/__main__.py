"""python -m stopline.proof --out <file.json> [--offline] [--backend real]."""

import argparse
import sys
import time
from pathlib import Path

from stopline import db as connection
from stopline.proof import summary
from stopline.proof.metrics import COUNT_KEYS
from stopline.proof.persistence import persist_and_reload, write_json
from stopline.proof.runner import finalize, run_suite
from stopline.proof.store import MemoryDatabase


def print_result(result):
    label = 'FAKE PROTOCOL MODEL' if result['backend'] == 'fake' else 'INTEGRATED COMPONENTS'
    print(label)
    print('Bounds: 2 workers, 2 steps each, depth 2 (root depth 0); orders 12 and 21.')
    print('Holds: ' + ', '.join(result['bounds']['hold_points']))
    print('mode      admit late-unsent in-flight missing duplicate bad-close delay-ms median/worst')
    for mode, counts in result['totals'].items():
        cells = ['n/a' if counts[k] is None else str(counts[k]) for k in COUNT_KEYS]
        delay = counts['added_delay_ms']
        print(f"{mode:<9} {' '.join(f'{v:>8}' for v in cells)}  {delay['median']}/{delay['worst']}")
    print(f"Schedules: {result['schedule_count']} names; {len(result['run'])} RUN; "
          f"{len(result['passed'])} PASSED; {len(result['failed'])} FAILED; {len(result['skipped'])} SKIPPED.")
    for row in result['rows']:
        status = row['status'].upper()
        prefix = 'RUN ' if status != 'SKIPPED' else ''
        reason = row.get('reason') or row.get('error')
        suffix = f' ({reason})' if reason else ''
        print(f"{prefix}{status}: {row['mode']}:{row['name']}{suffix}")
    if not result['failed']:
        print('FAILED: none')
    print('Flag counterexamples: ' + (', '.join(result['flag_counterexamples']) or 'none'))
    print(f"Elapsed: {result['elapsed_seconds']:.3f}s; one repetition of each applicable schedule.")
    print('Dispatch is measured separately from claim. In-flight arrivals are included above.')
    print('Added delay excludes holds and target I/O; measured fake timings are not Atlas gate latency.')
    cache = MemoryDatabase()
    cache['proof_results'].insert_one(result)
    print('\n'.join(summary(cache)))


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run finite schedules and publish evidence-derived counts.')
    parser.add_argument('--out', type=Path, required=True, help='JSON evidence and results file')
    parser.add_argument('--database', default=connection.DB_NAME, help='only proof_results is written here')
    parser.add_argument('--backend', choices=('fake', 'real'), default='fake')
    parser.add_argument('--offline', action='store_true', help='fakes plus JSON only; no Atlas reads or writes')
    parser.add_argument('--router-url', default='http://127.0.0.1:8766')
    args = parser.parse_args(argv)
    if args.offline and args.backend != 'fake':
        parser.error('--offline requires --backend fake')
    scratch = None
    exit_code = 2
    started = time.monotonic()
    try:
        if not args.offline:
            scratch = connection.test_database('proof')
        result = run_suite(backend=args.backend, scratch_db=scratch, router_url=args.router_url)
        if scratch is not None:
            if args.backend == 'fake':
                persist_and_reload(result, scratch)
            else:
                # The real gate already wrote its records. Target journals must be
                # collected by an integrated target logger before this mode is complete.
                result['sources']['record_storage'] = 'real scratch database; target snapshots captured locally'
            finalize(result)
            connection.drop_test_database(scratch)
            scratch = None
        result['elapsed_seconds'] = round(time.monotonic() - started, 3)
        result['duration_scope'] = 'execution, evidence write/read and scratch cleanup; excludes final JSON/result publication'
        result['under_six_minutes'] = result['elapsed_seconds'] < 360
        write_json(args.out, result)
        if not args.offline:
            connection.database(args.database)['proof_results'].insert_one(result)
        print_result(result)
        whole_seconds = time.monotonic() - started
        print(f'Total command wall time including publication: {whole_seconds:.3f}s')
        exit_code = 0 if result['complete'] and whole_seconds < 360 else 1
    except Exception as error:
        print(f'proof could not complete ({type(error).__name__}); no connection details logged', file=sys.stderr)
        exit_code = 2
    finally:
        if scratch is not None:
            try:
                connection.drop_test_database(scratch)
            except Exception as error:
                print(f'scratch cleanup failed ({type(error).__name__})', file=sys.stderr)
                exit_code = 2
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
