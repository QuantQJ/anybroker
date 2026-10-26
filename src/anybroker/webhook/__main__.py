"""The command line.

python -m anybroker.webhook secret
python -m anybroker.webhook serve  --config bridge.json
python -m anybroker.webhook check  --config bridge.json
"""

from __future__ import annotations

import argparse
import sys

from .config import Config, ConfigError, new_secret


def _load(args) -> Config:
    if args.config:
        return Config.from_file(args.config)
    return Config.from_env()


def cmd_secret(_args) -> int:
    secret = new_secret()
    print(secret)
    print()
    print('Put it in your config, then point TradingView at:')
    print('  https://your-host/alert/%s' % secret)
    print()
    print('TradingView cannot send custom headers, so the secret travels in')
    print('the URL. Use HTTPS, and prefer a reverse proxy over exposing this')
    print('process directly. The secret may also be sent as a "secret" field')
    print('in the JSON body, which keeps it out of access logs.')
    return 0


def cmd_check(args) -> int:
    """Validate the setup and connect, without listening or trading."""
    config = _load(args)
    print(config.describe())
    print()
    from .router import Router, RouterError

    router = Router(config)
    try:
        router.connect()
    except RouterError as exc:
        print('broker: FAILED  [%s] %s' % (exc.code, exc.message))
        return 1
    account = router.account
    print(
        'broker: connected. %s answered with account %s for %s.'
        % (
            config.venue,
            getattr(account, 'id', None) or '(unnamed)',
            config.env,
        )
    )
    return 0


def cmd_serve(args) -> int:
    config = _load(args)
    from .server import make_server

    server, bridge = make_server(config)
    print(config.describe())
    if config.is_live and not config.dry_run:
        print()
        print('  *** LIVE: orders from this endpoint reach a real account ***')
    print()
    try:
        bridge.start(connect=not args.no_connect)
    except Exception as exc:
        # The broker's own text can quote a credential it was handed, and
        # this line goes to a log file under launchd or systemd.
        print('could not start: %s' % bridge.router._safe(exc))
        return 1
    print('ready. ctrl-c to stop.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nstopping...')
    finally:
        server.shutdown()
        bridge.drain_now()
        bridge.stop()
    return 0


def cmd_journal(args) -> int:
    config = _load(args)
    from .journal import Journal

    print(Journal(config.journal).as_json(args.limit))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog='anyhook', description='TradingView alerts to any broker.'
    )
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('secret', help='generate a shared secret').set_defaults(
        func=cmd_secret
    )

    for name, func, helptext in (
        ('serve', cmd_serve, 'listen for alerts'),
        ('check', cmd_check, 'validate the setup and connect, without trading'),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument('--config', help='path to a JSON config file (mode 600)')
        if name == 'serve':
            p.add_argument(
                '--no-connect',
                action='store_true',
                help='listen without opening the broker (for a smoke test)',
            )
        p.set_defaults(func=func)

    p = sub.add_parser('journal', help='print the journal')
    p.add_argument('--config', help='path to a JSON config file')
    p.add_argument('--limit', type=int, default=50)
    p.set_defaults(func=cmd_journal)

    # Under launchd, systemd or `serve > log`, stdout is a file and block
    # buffered: the banner would sit in the buffer and an operator checking
    # the log of a running service would find it empty.
    reconfigure = getattr(sys.stdout, 'reconfigure', None)
    if callable(reconfigure):
        try:
            reconfigure(line_buffering=True)
        except ValueError:  # a closed or redirected-away stdout
            pass

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print('configuration: %s' % exc, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
