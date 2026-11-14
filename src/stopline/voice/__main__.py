"""Run a sound check without starting the demo or opening a database."""

import argparse

from . import say, warm
from .sentences import DEMO_SENTENCES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_mutually_exclusive_group(required=True)
    commands.add_argument('--say', metavar='TEXT', help='speak a sound check')
    commands.add_argument('--warm', action='store_true', help='cache the five demo clips without playing')
    args = parser.parse_args()
    if args.warm:
        ready = 0
        for future in warm(DEMO_SENTENCES):
            try:
                ready += future.result(timeout=15) is not None
            except Exception:
                pass
        print(f'{ready}/{len(DEMO_SENTENCES)} demo clips cached.')
        return 0
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
