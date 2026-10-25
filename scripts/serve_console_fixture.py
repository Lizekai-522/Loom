"""Loopback browser acceptance fixture with PUBLIC sentinel identities.

Never use these identities for a real project. No model gateway or executor is
started. Pass a disposable database outside the source/delivery directory.
"""
import argparse
from pathlib import Path
import uvicorn
from loom.api import create_app
from loom.auth import LocalIdentity
from loom.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8798)
    parser.add_argument('--local-console',action='store_true')
    args = parser.parse_args()
    if args.db.exists():
        parser.error('fixture requires a NEW disposable database; existing databases are rejected')
    identities = tuple(LocalIdentity('ui-fixture', role, frozenset({role}),
        'suite-console-public-fixture-only-' + role + '-00000000') for role in ('operator', 'worker', 'viewer'))
    app = create_app(Store(args.db), {'ui-fixture': 'suite-console-public-fixture-only-legacy-00000000'}, identities=identities,local_console=args.local_console)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
