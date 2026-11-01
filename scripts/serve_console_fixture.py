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
    parser.add_argument('--legacy-developer-keys',action='store_true',
                        help='Explicitly enable the historical public-sentinel developer fixture')
    args = parser.parse_args()
    if args.db.exists():
        parser.error('fixture requires a NEW disposable database; existing databases are rejected')
    identities = tuple(LocalIdentity('ui-fixture', role, frozenset({role}),
        'suite-console-public-fixture-only-' + role + '-00000000') for role in ('user', 'operator', 'worker', 'viewer'))
    legacy = {'ui-fixture': 'suite-console-public-fixture-only-legacy-00000000'} if args.legacy_developer_keys else {}
    app = create_app(Store(args.db), legacy, identities=identities,local_console=args.local_console,
                     legacy_developer_keys=args.legacy_developer_keys)
    uvicorn.run(app, host='127.0.0.1', port=args.port, access_log=False)


if __name__ == '__main__':
    main()
