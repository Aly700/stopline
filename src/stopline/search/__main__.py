"""Retrieve matching audit entries from an existing database."""
import argparse
import os

from stopline import db as storage

from .core import AuditSearch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('question')
    parser.add_argument('--run', default=None)
    parser.add_argument('--database', default=os.environ.get('STOPLINE_SEARCH_DATABASE', storage.DB_NAME))
    args = parser.parse_args(argv)
    try:
        search = AuditSearch(storage.database(args.database))
        result = search.ask(args.run, args.question)
    except Exception:
        print('Audit search is unavailable; check the database connection.')
        return 0
    if result['message']:
        print(result['message'])
    if result['ok']:
        print(f"Search: {result['engine']}")
        for row in result['rows']:
            print(f"{row['score']:.3f}  {row['text']}  [audit {row['audit_id']}]")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
