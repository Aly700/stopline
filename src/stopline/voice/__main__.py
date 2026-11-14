"""Run a sound check without starting the demo or opening a database."""

import argparse

from . import say


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--say', required=True, metavar='TEXT', help='speak a sound check')
    args = parser.parse_args()
    try:
        path = say(args.say).result(timeout=15)
    except Exception:
        path = None
    if path is None:
        print('Speech unavailable or disabled.')
    else:
        print('Sound check played; audio cached.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
