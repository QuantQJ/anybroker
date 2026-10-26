"""The bridge, end to end, with no broker and no network.

TradingView's documented constraints are what most of this is about: no
custom headers, a three-second cancel, four fixed senders, and redelivery of
the same alert.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import socket
import sys
import threading
import time

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from anybroker.webhook import (  # noqa: E402
    TRADINGVIEW_IPS,
    AlertError,
    Config,
    ConfigError,
    Journal,
    new_secret,
    parse_alert,
)
from anybroker.webhook.router import Router, RouterError  # noqa: E402
from anybroker.webhook.server import Bridge, make_server  # noqa: E402

SECRET = 'x' * 40


def _free_port() -> int:
    """A port the OS says is free, rather than one hoped to be."""
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def config(**kw):
    base = {
        'secret': SECRET,
        'venue': 'tradovate',
        'env': 'paper',
        'journal': None,
    }
    base.update(kw)
    return Config(**base)


# ══════════════════════════════════════════════════════ parsing the alert


def test_tradingviews_senders_are_the_documented_four():
    assert TRADINGVIEW_IPS == (
        '52.89.214.238',
        '34.212.75.30',
        '54.218.53.128',
        '52.32.178.7',
    )


def test_a_json_alert():
    a = parse_alert('{"action":"buy","symbol":"ABCZ6","qty":2}')
    assert a.action == 'buy' and a.symbol == 'ABCZ6' and a.qty == 2


def test_a_plain_text_alert():
    """TradingView sends text/plain when the message is not JSON."""
    a = parse_alert('buy 1 ABCZ6')
    assert a.action == 'buy' and a.qty == 1 and a.symbol == 'ABCZ6'


def test_a_plain_text_alert_with_a_price():
    a = parse_alert('sell 2 ABCZ6 @ 20150.25')
    assert a.action == 'sell' and a.qty == 2 and a.price == 20150.25


def test_pine_script_field_names_are_understood():
    """What a Pine strategy sends by default."""
    a = parse_alert(
        json.dumps(
            {
                'strategy.order.action': 'sell',
                'ticker': 'ABCZ6',
                'strategy.order.contracts': '3',
            }
        )
    )
    assert a.action == 'sell' and a.symbol == 'ABCZ6' and a.qty == 3


@pytest.mark.parametrize('word', ['buy', 'long', 'BUY', 'enter_long'])
def test_buy_synonyms(word):
    assert parse_alert('{"action":"%s","symbol":"X","qty":1}' % word).action in (
        word.lower(),
    )


def test_a_bracket_alert():
    a = parse_alert(
        '{"action":"buy","symbol":"ABCZ6","qty":1,"stop":20100,"take_profit":20200}'
    )
    assert a.stop == 20100 and a.take_profit == 20200


def test_an_empty_body_is_refused():
    with pytest.raises(AlertError) as caught:
        parse_alert('   ')
    assert caught.value.code == 'EMPTY'


def test_a_missing_side_is_refused_rather_than_assumed():
    with pytest.raises(AlertError) as caught:
        parse_alert('{"symbol":"ABCZ6","qty":1}')
    assert caught.value.code == 'NO_ACTION'


def test_a_missing_symbol_is_refused():
    with pytest.raises(AlertError) as caught:
        parse_alert('{"action":"buy","qty":1}')
    assert caught.value.code == 'NO_SYMBOL'


def test_a_missing_size_is_refused_when_no_default_is_set():
    """Guessing a size is how an alert becomes the wrong trade."""
    with pytest.raises(AlertError) as caught:
        parse_alert('{"action":"buy","symbol":"ABCZ6"}')
    assert caught.value.code == 'NO_QTY'


def test_a_configured_default_size_is_used():
    a = parse_alert('{"action":"buy","symbol":"ABCZ6"}', default_qty=2)
    assert a.qty == 2


def test_a_flatten_needs_no_size():
    a = parse_alert('{"action":"flatten","symbol":"ABCZ6"}')
    assert a.is_flatten and a.qty is None


def test_an_unknown_action_is_refused_not_interpreted():
    with pytest.raises(AlertError) as caught:
        parse_alert('{"action":"maybe","symbol":"X","qty":1}')
    assert caught.value.code == 'BAD_ACTION'


def test_a_negative_size_is_refused():
    with pytest.raises(AlertError) as caught:
        parse_alert('{"action":"buy","symbol":"X","qty":-1}')
    assert caught.value.code == 'BAD_QTY'


def test_broken_json_says_so():
    with pytest.raises(AlertError) as caught:
        parse_alert('{"action":"buy",,}')
    assert caught.value.code == 'BAD_JSON'


# ── redelivery ─────────────────────────────────────────────────────────────


def test_the_same_alert_gets_the_same_id():
    """TradingView sends no id and can deliver twice."""
    body = '{"action":"buy","symbol":"ABCZ6","qty":1}'
    assert parse_alert(body).order_id == parse_alert(body).order_id


def test_a_different_alert_gets_a_different_id():
    a = parse_alert('{"action":"buy","symbol":"ABCZ6","qty":1}')
    b = parse_alert('{"action":"buy","symbol":"ABCZ6","qty":2}')
    assert a.order_id != b.order_id


def test_an_alert_can_carry_its_own_id():
    a = parse_alert('{"action":"buy","symbol":"X","qty":1,"id":"mine-1"}')
    assert a.order_id == 'mine-1'


# ══════════════════════════════════════════════════════ configuration


def test_a_secret_is_required():
    with pytest.raises(ConfigError, match='shared secret'):
        config(secret=None)


def test_a_short_secret_is_refused():
    with pytest.raises(ConfigError, match='shared secret'):
        config(secret='short')


def test_the_environment_must_be_spelled_out():
    for bad in (None, '', 'prod', 'demo'):
        with pytest.raises(ConfigError, match='paper'):
            config(env=bad)


def test_a_live_endpoint_open_to_everyone_is_refused():
    with pytest.raises(ConfigError, match='refused'):
        config(env='live', allow_any_ip=True)


def test_an_open_paper_endpoint_is_allowed():
    assert config(env='paper', allow_any_ip=True).allow_any_ip


def test_the_senders_default_to_tradingviews():
    from anybroker.webhook.config import DEFAULT_ALLOW_IPS

    assert config().allow_ips == DEFAULT_ALLOW_IPS == TRADINGVIEW_IPS
    assert '8.8.8.8' not in config().allow_ips


def test_the_banner_never_prints_the_secret():
    assert SECRET not in config().describe()


def test_the_url_path_carries_the_secret():
    assert config().url_path == '/alert/' + SECRET


def test_a_world_readable_config_is_refused(tmp_path):
    path = tmp_path / 'bridge.json'
    path.write_text(json.dumps({'secret': SECRET, 'venue': 'x', 'env': 'paper'}))
    path.chmod(0o644)
    with pytest.raises(ConfigError, match='readable by others'):
        Config.from_file(path)


def test_a_generated_secret_is_long_enough():
    assert len(new_secret()) >= 24


# ══════════════════════════════════════════════════════ the router


class FakeVenue:
    def __init__(self, *, raises=None):
        self.placed = []
        self.flattened = []
        self._raises = raises
        self.connected = False

    def connect(self):
        self.connected = True
        return self

    def close(self):
        self.connected = False

    def place(self, spec):
        if self._raises:
            raise self._raises
        self.placed.append(spec)
        return type('O', (), {'id': 'B%d' % len(self.placed)})()

    def flatten(self, symbol):
        self.flattened.append(symbol)
        return type('O', (), {'id': 'F1'})()

    def list_positions(self):
        return []

    def working_orders(self):
        return []

    def unprotected_positions(self):
        return []

    def unresolved(self):
        return []


def router(venue=None, **kw):
    return Router(config(**kw), venue=venue or FakeVenue())


needs_anybroker = pytest.mark.skipif(
    importlib.util.find_spec('anybroker') is None,
    reason='anybroker is an optional extra and is not on PyPI yet',
)


@needs_anybroker
def test_a_market_alert_becomes_a_market_order():
    from anybroker import OrderType, Side

    v = FakeVenue()
    router(v).place(parse_alert('{"action":"buy","symbol":"ABCZ6","qty":2}'))
    spec = v.placed[-1]
    assert spec.order_type is OrderType.MARKET
    assert spec.side is Side.BUY and spec.qty == 2


@needs_anybroker
def test_a_price_makes_it_a_limit():
    from anybroker import OrderType

    v = FakeVenue()
    router(v).place(parse_alert('buy 1 ABCZ6 @ 20150.25'))
    assert v.placed[-1].order_type is OrderType.LIMIT
    assert v.placed[-1].limit_price == 20150.25


@needs_anybroker
def test_a_stop_makes_it_a_stop():
    from anybroker import OrderType

    v = FakeVenue()
    router(v).place(
        parse_alert('{"action":"sell","symbol":"ABCZ6","qty":1,"stop":20100}')
    )
    assert v.placed[-1].order_type is OrderType.STOP


@needs_anybroker
def test_both_prices_make_it_a_stop_limit():
    from anybroker import OrderType

    v = FakeVenue()
    router(v).place(
        parse_alert(
            '{"action":"sell","symbol":"ABCZ6","qty":1,"price":20095,"stop":20100}'
        )
    )
    assert v.placed[-1].order_type is OrderType.STOP_LIMIT


@needs_anybroker
def test_a_take_profit_makes_it_a_bracket():
    from anybroker import Side

    v = FakeVenue()
    router(v).place(
        parse_alert(
            '{"action":"buy","symbol":"ABCZ6","qty":1,"stop":20100,"take_profit":20200}'
        )
    )
    spec = v.placed[-1]
    assert spec.is_bracket
    assert spec.take_profit == 20200 and spec.stop_loss == 20100
    assert spec.exit_side is Side.SELL


@needs_anybroker
def test_the_alert_id_becomes_the_client_order_id():
    """So a redelivery is refused by the broker layer, not filled twice."""
    v = FakeVenue()
    alert = parse_alert('{"action":"buy","symbol":"ABCZ6","qty":1}')
    router(v).place(alert)
    assert v.placed[-1].client_order_id == alert.order_id


@needs_anybroker
def test_a_flatten_closes_rather_than_ordering():
    v = FakeVenue()
    router(v).place(parse_alert('{"action":"flatten","symbol":"ABCZ6"}'))
    assert v.flattened == ['ABCZ6'] and v.placed == []


@needs_anybroker
def test_a_dry_run_sends_nothing():
    v = FakeVenue()
    result = router(v, dry_run=True).place(
        parse_alert('{"action":"buy","symbol":"ABCZ6","qty":1}')
    )
    assert result['outcome'] == 'dry_run'
    assert v.placed == []


@needs_anybroker
def test_a_duplicate_is_named_as_such():
    from anybroker import DuplicateOrderError

    v = FakeVenue(raises=DuplicateOrderError('already sent'))
    with pytest.raises(RouterError) as caught:
        router(v).place(parse_alert('{"action":"buy","symbol":"X","qty":1}'))
    assert caught.value.code == 'DUPLICATE'
    assert caught.value.ambiguous is False


@needs_anybroker
def test_an_unresolved_order_is_marked_ambiguous():
    """An alert whose fate is unknown must not be replayed."""
    from anybroker import UnresolvedOrderError

    v = FakeVenue(raises=UnresolvedOrderError('never answered'))
    with pytest.raises(RouterError) as caught:
        router(v).place(parse_alert('{"action":"buy","symbol":"X","qty":1}'))
    assert caught.value.code == 'UNRESOLVED'
    assert caught.value.ambiguous is True


@needs_anybroker
def test_a_rejection_is_not_ambiguous():
    from anybroker import RejectedError

    v = FakeVenue(raises=RejectedError('no margin'))
    with pytest.raises(RouterError) as caught:
        router(v).place(parse_alert('{"action":"buy","symbol":"X","qty":1}'))
    assert caught.value.code == 'REJECTED'
    assert caught.value.ambiguous is False


# ══════════════════════════════════════════════════════ the bridge


def bridge(venue=None, **kw):
    venue = venue or FakeVenue()
    cfg = config(**kw)
    return Bridge(cfg, router=Router(cfg, venue=venue)), venue


@needs_anybroker
def test_a_good_alert_is_accepted_immediately():
    b, v = bridge()
    status, payload = b.accept('{"action":"buy","symbol":"ABCZ6","qty":1}')
    assert status == 202
    assert payload['status'] == 'accepted'
    assert 'order_id' in payload


@needs_anybroker
def test_the_reply_does_not_claim_a_fill():
    """Nothing truthful can say 'filled' inside TradingView's three seconds."""
    b, _ = bridge()
    _, payload = b.accept('buy 1 ABCZ6')
    assert 'accepted for routing' in payload['note']
    assert 'filled' not in json.dumps(payload).lower()


@needs_anybroker
def test_accepting_is_fast_because_nothing_slow_happens_on_it():
    b, _ = bridge()
    start = time.time()
    for _ in range(200):
        b.accept('buy 1 ABCZ6')
    assert time.time() - start < 1.0, 'the hot path must not touch a broker'


@needs_anybroker
def test_a_bad_alert_is_refused_with_a_reason():
    b, v = bridge()
    status, payload = b.accept('{"symbol":"ABCZ6"}')
    assert status == 400
    assert payload['code'] == 'NO_ACTION'
    assert v.placed == []


@needs_anybroker
def test_the_worker_places_what_was_accepted():
    b, v = bridge()
    b.start(connect=False)
    try:
        b.accept('{"action":"buy","symbol":"ABCZ6","qty":1}')
        b.drain_now()
        time.sleep(0.05)
        assert len(v.placed) == 1
        assert b.placed == 1
    finally:
        b.stop()


@needs_anybroker
def test_a_refusal_is_in_the_journal_with_its_reason():
    b, _ = bridge()
    b.accept('{"symbol":"ABCZ6"}')
    refusals = b.journal.refusals()
    assert refusals and refusals[0]['code'] == 'NO_ACTION'


@needs_anybroker
def test_the_journal_records_both_sides():
    b, _ = bridge()
    b.accept('buy 1 ABCZ6')
    b.accept('nonsense')
    counts = b.journal.counts()
    assert counts.get('accepted') == 1
    assert counts.get('refused') == 1


@needs_anybroker
def test_a_broker_failure_does_not_kill_the_worker():
    from anybroker import RejectedError

    b, v = bridge(venue=FakeVenue(raises=RejectedError('no')))
    b.start(connect=False)
    try:
        b.accept('buy 1 ABCZ6')
        b.drain_now()
        time.sleep(0.05)
        b.accept('buy 2 ABCZ6')
        b.drain_now()
        time.sleep(0.05)
        assert b.refused == 2, 'the second alert still got processed'
    finally:
        b.stop()


@needs_anybroker
def test_status_is_read_only_and_never_leaks_the_secret():
    b, _ = bridge()
    text = json.dumps(b.status(), default=str)
    assert SECRET not in text
    assert b.status()['env'] == 'paper'


# ══════════════════════════════════════════════════════ the journal


@needs_anybroker
def test_the_journal_persists(tmp_path):
    path = tmp_path / 'j.sqlite'
    j = Journal(path)
    j.record('accepted', code='X', detail='d')
    j.close()
    assert Journal(path).counts() == {'accepted': 1}


@needs_anybroker
def test_the_journal_never_raises_a_trade_over_a_write(tmp_path):
    j = Journal(tmp_path / 'j.sqlite')
    j.close()
    assert j.record('placed') == 0, 'a closed journal returns 0 rather than raising'


# ── a redelivered alert does not become a second order ──────────────────────
#
# These drive anybroker's real idempotency layer: only the wire call is
# stubbed, so the deduplication under test is the one that runs in production.


def real_venue():
    from anybroker import Env
    from anybroker.models import Order
    from anybroker.venue import Capabilities, Venue

    class StubWire(Venue):
        """A real anybroker venue with only the HTTP call replaced."""

        name = 'stub'
        capabilities = Capabilities(supports_paper=True, supports_brackets=True)

        def __init__(self):
            super().__init__(Env.PAPER)
            self.sent = []

        def _place(self, spec):
            self.sent.append(spec)
            return Order(
                id='B%d' % len(self.sent),
                venue='stub',
                symbol=spec.symbol,
                side=spec.side,
                qty=spec.qty,
                order_type=spec.order_type,
                status='Working',
            )

        def get_order(self, order_id):
            return None

        def list_positions(self):
            return []

        def list_orders(self):
            return []

    return StubWire()


def deliver(b, *bodies):
    """Accept each body and wait for the worker to finish with it."""
    b.start(connect=False)
    try:
        for body in bodies:
            assert b.accept(body)[0] == 202
        b.drain_now()
        time.sleep(0.05)
    finally:
        b.stop()


@needs_anybroker
def test_redelivered_alert_sends_one_order():
    v = real_venue()
    b, _ = bridge(venue=v)
    body = '{"action":"buy","symbol":"ABCZ6","qty":2}'
    deliver(b, body, body, body)

    assert len(v.sent) == 1, 'a redelivery reached the wire'


@needs_anybroker
def test_journal_does_not_count_a_redelivery_as_a_placement():
    """The count has to be orders sent, not alerts received.

    anybroker answers a redelivery with the live order and sends nothing,
    which at the call site is indistinguishable from a fresh placement.
    Recorded as ``placed`` it would say three orders went out when one did.
    """
    v = real_venue()
    b, _ = bridge(venue=v)
    body = '{"action":"buy","symbol":"ABCZ6","qty":2}'
    deliver(b, body, body, body)

    assert b.placed == 1
    assert b.duplicates == 2
    counts = b.journal.counts()
    assert counts.get('placed') == 1
    assert counts.get('duplicate') == 2
    assert b.status()['accepted'] == 3


@needs_anybroker
def test_a_different_size_is_a_different_order():
    """Identity is the instruction, so a genuine second signal still goes."""
    v = real_venue()
    b, _ = bridge(venue=v)
    deliver(
        b,
        '{"action":"buy","symbol":"ABCZ6","qty":2}',
        '{"action":"buy","symbol":"ABCZ6","qty":3}',
    )

    assert len(v.sent) == 2
    assert b.duplicates == 0


# ── the operator can reach their own bridge ─────────────────────────────────


def test_an_address_off_the_list_is_still_refused():
    cfg = config()
    assert '203.0.113.7' not in cfg.allow_ips
    assert not cfg.allow_any_ip


def test_healthz_answers_the_machine_itself_with_an_empty_allowlist():
    """A launchd probe has no secret and may not be on the list."""
    import http.client

    cfg = config(port=_free_port(), allow_ips=())
    server, bridge = make_server(cfg)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection('127.0.0.1', cfg.port, timeout=3)
        conn.request('GET', '/healthz')
        response = conn.getresponse()
        assert response.status == 200
        assert json.loads(response.read())['status'] == 'ok'
        # The alert endpoint is not ungated with it.
        conn = http.client.HTTPConnection('127.0.0.1', cfg.port, timeout=3)
        conn.request('POST', cfg.url_path, body='{"action":"buy","symbol":"X","qty":1}')
        assert conn.getresponse().status == 404
    finally:
        server.shutdown()
        server.server_close()


# ── the sender check, and what a proxy in front of it does ─────────────────


def _serve(cfg):
    """Run a real server on a free port. Returns (server, bridge)."""
    server, bridge = make_server(cfg)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, bridge


def _post(cfg, body='{"action":"buy","symbol":"ABCZ6","qty":1}', path=None, **headers):
    import http.client

    conn = http.client.HTTPConnection('127.0.0.1', cfg.port, timeout=3)
    try:
        conn.request('POST', path or cfg.url_path, body=body, headers=headers)
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_loopback_is_not_in_the_default_allowlist():
    """A reverse proxy on the same box connects from loopback.

    Allowing loopback by default would accept whatever that proxy forwards,
    from any address, and the sender check would quietly stop meaning
    anything -- in exactly the deployment the README recommends.
    """
    cfg = config()
    assert '127.0.0.1' not in cfg.allow_ips
    assert '::1' not in cfg.allow_ips
    assert cfg.allow_ips == TRADINGVIEW_IPS


def test_a_local_post_is_refused_unless_asked_for():
    cfg = config(port=_free_port())
    server, _ = _serve(cfg)
    try:
        assert _post(cfg)[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_an_undeclared_peer_cannot_claim_an_address():
    """X-Forwarded-For from a peer that is not a declared proxy is ignored."""
    cfg = config(port=_free_port())
    server, _ = _serve(cfg)
    try:
        status, _ = _post(cfg, **{'X-Forwarded-For': TRADINGVIEW_IPS[0]})
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_a_declared_proxy_has_its_forwarded_client_checked():
    cfg = config(port=_free_port(), trusted_proxies=('127.0.0.1',))
    server, bridge = _serve(cfg)
    try:
        allowed, _ = _post(cfg, **{'X-Forwarded-For': TRADINGVIEW_IPS[0]})
        assert allowed == 202
        refused, _ = _post(cfg, **{'X-Forwarded-For': '203.0.113.9'})
        assert refused == 404
        # Only the hop the proxy appended counts; the rest is client-supplied.
        spoofed, _ = _post(
            cfg, **{'X-Forwarded-For': '%s, 203.0.113.9' % TRADINGVIEW_IPS[0]}
        )
        assert spoofed == 404
    finally:
        server.shutdown()
        server.server_close()


def test_a_negative_content_length_is_refused():
    """int('-1') passes a "too large?" test, then read(-1) runs to EOF."""
    cfg = config(port=_free_port(), allow_any_ip=True)
    server, _ = _serve(cfg)
    try:
        status, _ = _post(cfg, body='', **{'Content-Length': '-1'})
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()


# ── the secret does not reach the journal ──────────────────────────────────


def test_a_secret_in_the_payload_is_not_journaled():
    """The journal is a file people attach to a bug report."""
    from anybroker.webhook import redact_secret

    body = json.dumps({'secret': SECRET, 'action': 'buy', 'symbol': 'X', 'qty': 1})
    cleaned = redact_secret(body, SECRET)
    assert SECRET not in cleaned
    assert json.loads(cleaned)['action'] == 'buy'


def test_a_secret_in_a_plain_text_body_is_redacted():
    from anybroker.webhook import redact_secret

    cleaned = redact_secret('buy 1 ABCZ6 %s' % SECRET, SECRET)
    assert SECRET not in cleaned
    assert '<redacted>' in cleaned


def test_the_journal_never_holds_the_secret_end_to_end():
    cfg = config(port=_free_port(), allow_any_ip=True)
    server, bridge = _serve(cfg)
    try:
        body = json.dumps({'secret': SECRET, 'action': 'buy', 'symbol': 'X'})
        _post(cfg, body=body, path=cfg.path)  # no size: refused, journaled
        rows = json.dumps(bridge.journal.recent(10))
        assert SECRET not in rows
    finally:
        server.shutdown()
        server.server_close()


def test_the_journal_file_is_private(tmp_path):
    path = tmp_path / 'j.sqlite'
    Journal(path).close()
    assert path.stat().st_mode & 0o077 == 0, 'the journal is readable by others'


def test_an_existing_loose_journal_is_tightened(tmp_path):
    path = tmp_path / 'j.sqlite'
    path.touch(mode=0o644)
    Journal(path).close()
    assert path.stat().st_mode & 0o077 == 0


# ── nothing is left in the pipe after a refusal ────────────────────────────
#
# This is HTTP/1.1: the connection is reused. A refusal that answers without
# reading the body leaves those bytes to be parsed as the next request on
# that same connection -- past the sender and secret checks.


def _raw(cfg, request_bytes, reads=2):
    """Send bytes on one socket and read what comes back."""
    sock = socket.create_connection(('127.0.0.1', cfg.port), timeout=3)
    try:
        sock.sendall(request_bytes)
        chunks = []
        for _ in range(reads):
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        return b''.join(chunks)
    finally:
        sock.close()


def test_a_refused_sender_cannot_smuggle_a_second_request():
    cfg = config(port=_free_port(), journal=None)
    server, bridge = _serve(cfg)
    try:
        smuggled = (
            'POST %s HTTP/1.1\r\n'
            'Host: x\r\n'
            'Content-Length: 41\r\n'
            '\r\n'
            '{"action":"buy","symbol":"ABCZ6","qty":1}'
        ) % cfg.url_path
        reply = _raw(cfg, smuggled.encode())
        assert b'404' in reply
        assert b'Connection: close' in reply
        # One answer, not two: the body was not read back as a request.
        assert reply.count(b'HTTP/1.') == 1
        assert bridge.placed == 0
    finally:
        server.shutdown()
        server.server_close()


def test_an_oversized_body_closes_the_connection():
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        head = ('POST %s HTTP/1.1\r\nHost: x\r\nContent-Length: %d\r\n\r\n') % (
            cfg.url_path,
            64 * 1024 + 1,
        )
        reply = _raw(cfg, head.encode())
        assert b'413' in reply
        assert b'Connection: close' in reply
    finally:
        server.shutdown()
        server.server_close()


def test_a_chunked_body_is_refused():
    """No Content-Length means nothing would read it."""
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, bridge = _serve(cfg)
    try:
        request = (
            'POST %s HTTP/1.1\r\n'
            'Host: x\r\n'
            'Transfer-Encoding: chunked\r\n'
            '\r\n'
            '29\r\n{"action":"buy","symbol":"ABCZ6","qty":1}\r\n0\r\n\r\n'
        ) % cfg.url_path
        reply = _raw(cfg, request.encode())
        assert b'400' in reply
        assert b'TRANSFER_ENCODING' in reply
        assert bridge.placed == 0
    finally:
        server.shutdown()
        server.server_close()


def test_a_client_cannot_add_its_own_forwarded_header():
    """Two X-Forwarded-For lines: the proxy's is the one that counts.

    ``headers.get`` returns the *first* occurrence, so a client that sends
    its own line would be the one believed.
    """
    cfg = config(port=_free_port(), trusted_proxies=('127.0.0.1',), journal=None)
    server, _ = _serve(cfg)
    try:
        request = (
            'POST %s HTTP/1.1\r\n'
            'Host: x\r\n'
            'X-Forwarded-For: %s\r\n'  # the client's own claim, first
            'X-Forwarded-For: 203.0.113.9\r\n'  # what the proxy appended
            'Content-Length: 41\r\n'
            '\r\n'
            '{"action":"buy","symbol":"ABCZ6","qty":1}'
        ) % (cfg.url_path, TRADINGVIEW_IPS[0])
        reply = _raw(cfg, request.encode())
        assert b'404' in reply, 'the client-supplied header was believed'
    finally:
        server.shutdown()
        server.server_close()


def test_only_the_named_forwarded_header_is_read():
    """X-Real-IP is not consulted when the operator named X-Forwarded-For."""
    cfg = config(port=_free_port(), trusted_proxies=('127.0.0.1',), journal=None)
    server, _ = _serve(cfg)
    try:
        status, _ = _post(cfg, **{'X-Real-IP': TRADINGVIEW_IPS[0]})
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_operator_can_name_the_header_their_proxy_sets():
    cfg = config(
        port=_free_port(),
        trusted_proxies=('127.0.0.1',),
        forwarded_header='X-Real-IP',
        journal=None,
    )
    server, _ = _serve(cfg)
    try:
        assert _post(cfg, **{'X-Real-IP': TRADINGVIEW_IPS[0]})[0] == 202
        assert _post(cfg, **{'X-Forwarded-For': TRADINGVIEW_IPS[0]})[0] == 404
    finally:
        server.shutdown()
        server.server_close()


def test_the_secret_is_redacted_even_when_it_hides_elsewhere():
    from anybroker.webhook import redact_secret

    nested = json.dumps({'secret': SECRET, 'note': 'key is %s' % SECRET})
    assert SECRET not in redact_secret(nested, SECRET)

    inner = json.dumps({'action': 'buy', 'meta': {'secret': SECRET}})
    assert SECRET not in redact_secret(inner, SECRET)


# ── the secret does not come back in a refusal ─────────────────────────────


def test_a_secret_in_the_wrong_field_is_not_echoed_or_journaled():
    """An alert that puts the secret where a number belongs.

    The refusal quotes the value it rejected, and that text goes to both the
    journal and the HTTP response.
    """
    b, _ = bridge(allow_any_ip=True, journal=None)
    status, payload = b.accept(
        json.dumps({'action': 'buy', 'symbol': 'ABCZ6', 'qty': SECRET})
    )

    assert status == 400
    assert SECRET not in json.dumps(payload)
    assert SECRET not in json.dumps(b.journal.recent(5))


def test_a_secret_in_an_unrecognised_action_is_not_echoed():
    b, _ = bridge(allow_any_ip=True, journal=None)
    status, payload = b.accept(json.dumps({'action': SECRET, 'symbol': 'X', 'qty': 1}))

    assert status == 400
    assert SECRET not in json.dumps(payload)
    assert SECRET not in json.dumps(b.journal.recent(5))


def test_the_secret_is_compared_in_constant_time():
    """Not a timing measurement -- that is unmeasurable here.

    This asserts the code path uses the constant-time compare, so a later
    edit back to ``==`` fails a test rather than passing quietly.
    """
    import inspect

    from anybroker.webhook import server as server_module

    source = inspect.getsource(server_module)
    assert 'hmac.compare_digest' in source
    authorised = source[source.index('def _authorised') :][:600]
    assert '_same(' in authorised
    assert '== config.secret' not in authorised


def test_a_wrong_secret_of_the_right_length_is_still_refused():
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        wrong = '%s/alert/%s' % ('', 'y' * len(SECRET))
        status, _ = _post(cfg, path=wrong)
        assert status == 404
    finally:
        server.shutdown()
        server.server_close()


# ── "connected" has to mean the broker answered ────────────────────────────


def test_connect_reads_the_account_back():
    """A venue can build its client without talking to the broker.

    Several do -- the token is fetched at the first request. So a venue that
    constructed cleanly proves only that the credentials parsed, and a check
    that stops there reports success for a setup that cannot trade.
    """

    class LazyVenue(FakeVenue):
        def __init__(self):
            self.connected = False
            self.account_reads = 0
            super().__init__()

        def connect(self):
            self.connected = True  # no broker contact at all
            return self

        def get_account(self):
            self.account_reads += 1
            return type('A', (), {'id': 'DEMO123', 'name': 'demo'})()

    v = LazyVenue()
    r = router(v)
    r.connect()
    assert v.connected
    assert v.account_reads == 1, 'connect did not read the account back'
    assert r.account.id == 'DEMO123'


def test_connect_fails_when_the_account_read_is_rejected():
    """The failure the real Tradovate demo gave: auth refused on first read."""

    class RefusingVenue(FakeVenue):
        def connect(self):
            return self

        def get_account(self):
            raise RuntimeError('Tradovate refused the login: not registered')

    with pytest.raises(RouterError) as caught:
        router(RefusingVenue()).connect()
    assert caught.value.code == 'ACCOUNT_READ_FAILED'
    assert 'refused' in caught.value.message


def test_connect_refuses_a_different_account_than_configured():
    class OtherAccountVenue(FakeVenue):
        def connect(self):
            return self

        def get_account(self):
            return type('A', (), {'id': 'SOMEONE_ELSE', 'name': 'other'})()

    with pytest.raises(RouterError) as caught:
        router(OtherAccountVenue(), account='MINE').connect()
    assert caught.value.code == 'WRONG_ACCOUNT'


# ── nothing reaches the journal carrying the secret ────────────────────────


def test_the_journal_cleans_every_field_it_is_given():
    """One place, so a field added later is covered by default."""
    j = Journal(None, secret=SECRET)
    j.record(
        'refused',
        code='X_%s' % SECRET,
        detail='broker said: %s' % SECRET,
        venue=SECRET,
        env='paper',
        broker_id=SECRET,
        source_ip=SECRET,
        body=SECRET,
    )
    assert SECRET not in json.dumps(j.recent(5))


def test_an_alerts_own_fields_are_cleaned_too():
    """A symbol or an id is free text: it can carry whatever was sent."""
    j = Journal(None, secret=SECRET)
    alert = parse_alert(
        json.dumps({'action': 'buy', 'symbol': SECRET, 'qty': 1, 'id': SECRET})
    )
    j.record('accepted', alert=alert, venue='v', env='paper')
    assert SECRET not in json.dumps(j.recent(5))


def test_a_broker_exception_quoting_a_credential_is_cleaned():
    """RouterError wraps str(exc) from the venue, which saw the credentials."""
    cfg = config(journal=None)

    class LeakyVenue(FakeVenue):
        def place(self, spec):
            raise RuntimeError('auth failed with sec=%s' % SECRET)

    b = Bridge(cfg, router=Router(cfg, venue=LeakyVenue()))
    b.start(connect=False)
    try:
        b.accept('{"action":"buy","symbol":"ABCZ6","qty":1}')
        b.drain_now()
        time.sleep(0.05)
    finally:
        b.stop()

    assert SECRET not in json.dumps(b.journal.recent(5))


def test_the_dry_run_detail_is_cleaned():
    b, _ = bridge(dry_run=True, journal=None)
    b.start(connect=False)
    try:
        b.accept(json.dumps({'action': 'buy', 'symbol': SECRET, 'qty': 1}))
        b.drain_now()
        time.sleep(0.05)
    finally:
        b.stop()

    assert SECRET not in json.dumps(b.journal.recent(5))


# ── a smuggler cannot make the length parse one way and read another ───────


def test_an_underscored_content_length_is_refused():
    """int('5_0') is 50 in Python, so a coerced length is not the byte count."""
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        request = (
            'POST %s HTTP/1.1\r\nHost: x\r\nContent-Length: 5_0\r\n\r\n'
        ) % cfg.url_path
        reply = _raw(cfg, request.encode())
        assert b'400' in reply and b'BAD_LENGTH' in reply
    finally:
        server.shutdown()
        server.server_close()


def test_a_signed_or_non_ascii_content_length_is_refused():
    """int() reads '+5' as 5 and the fullwidth '５' as 5 as well.

    Surrounding whitespace is not in here: the header parser strips it before
    the handler sees the value, so ' 5 ' never reaches this check.
    """
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        for value in ('+5', '0x5', '５'):
            request = ('POST %s HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\n') % (
                cfg.url_path,
                value,
            )
            reply = _raw(cfg, request.encode('utf-8'))
            assert b'400' in reply, 'accepted Content-Length %r' % value
    finally:
        server.shutdown()
        server.server_close()


def test_two_disagreeing_content_lengths_are_refused():
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        request = (
            'POST %s HTTP/1.1\r\nHost: x\r\n'
            'Content-Length: 5\r\nContent-Length: 41\r\n\r\n'
            '{"action":"buy","symbol":"ABCZ6","qty":1}'
        ) % cfg.url_path
        reply = _raw(cfg, request.encode())
        assert b'400' in reply and b'BAD_LENGTH' in reply
    finally:
        server.shutdown()
        server.server_close()


def test_a_get_carrying_a_body_is_refused():
    """Nothing in do_GET reads a body, so a declared one would be left over."""
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        request = (
            'GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: 41\r\n\r\n'
            '{"action":"buy","symbol":"ABCZ6","qty":1}'
        )
        reply = _raw(cfg, request.encode())
        assert b'400' in reply and b'BODY_ON_GET' in reply
        assert reply.count(b'HTTP/1.') == 1
    finally:
        server.shutdown()
        server.server_close()


# ── broker credentials are not the webhook secret, and also must not leak ──


def test_venue_credentials_are_treated_as_sensitive():
    cfg = config(
        venue_options={
            'apiKey': 'AKIAEXAMPLEKEY1234',
            'api_secret': 'shhh-this-is-the-secret-value',
            'credential_path': '/Users/me/.tradovate_key',
            'account': 'ACC1',
        }
    )
    values = cfg.sensitive_values()
    assert 'AKIAEXAMPLEKEY1234' in values
    assert 'shhh-this-is-the-secret-value' in values
    assert cfg.secret in values
    # A path names a file rather than holding one, and blanking it would hide
    # the useful half of "no credential file at ...".
    assert '/Users/me/.tradovate_key' not in values
    assert 'ACC1' not in values


def test_nested_credentials_are_found():
    cfg = config(
        venue_options={'credentials': {'password': 'hunter2-hunter2', 'name': 'bob'}}
    )
    values = cfg.sensitive_values()
    assert 'hunter2-hunter2' in values
    assert 'bob' not in values


def test_a_broker_error_quoting_a_key_is_cleaned_in_status():
    key = 'AKIAEXAMPLEKEY1234'
    cfg = config(venue_options={'apiKey': key}, journal=None)

    class LeakyVenue(FakeVenue):
        def list_positions(self):
            raise RuntimeError('rejected: apiKey=%s is invalid' % key)

    out = Router(cfg, venue=LeakyVenue()).status()
    assert key not in json.dumps(out)
    assert 'RuntimeError' in out['positions_error']


def test_a_broker_error_quoting_a_key_is_cleaned_in_the_journal():
    key = 'AKIAEXAMPLEKEY1234'
    cfg = config(venue_options={'apiKey': key}, journal=None)

    class LeakyVenue(FakeVenue):
        def place(self, spec):
            raise RuntimeError('rejected: apiKey=%s is invalid' % key)

    b = Bridge(cfg, router=Router(cfg, venue=LeakyVenue()))
    b.start(connect=False)
    try:
        b.accept('{"action":"buy","symbol":"ABCZ6","qty":1}')
        b.drain_now()
        time.sleep(0.05)
    finally:
        b.stop()

    assert key not in json.dumps(b.journal.recent(5))


def test_a_body_that_never_arrives_does_not_hold_the_thread_forever():
    """A request may announce a body and then send nothing."""
    from anybroker.webhook.server import READ_TIMEOUT

    assert READ_TIMEOUT and READ_TIMEOUT <= 60
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        sock = socket.create_connection(('127.0.0.1', cfg.port), timeout=3)
        try:
            sock.sendall(
                (
                    'POST %s HTTP/1.1\r\nHost: x\r\nContent-Length: 500\r\n\r\n'
                    % cfg.url_path
                ).encode()
            )
            # The handler is now waiting on a body that is not coming. The
            # server must still answer somebody else.
            assert _post(cfg)[0] == 202
        finally:
            sock.close()
    finally:
        server.shutdown()
        server.server_close()


# ── the malformed-length bypass ────────────────────────────────────────────


def test_a_get_with_a_malformed_length_is_refused_too():
    """The bypass a truthiness test leaves open.

    A malformed Content-Length is reported as None, and None is falsy -- so
    ``if self._declared_length():`` reads "Content-Length: 5_0" as no body
    and lets through exactly what the check exists to stop.
    """
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        for value in ('5_0', '+5', '0x5'):
            request = (
                'GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: %s\r\n\r\n'
                'GET %s/status HTTP/1.1\r\nHost: x\r\n\r\n'
            ) % (value, cfg.url_path)
            reply = _raw(cfg, request.encode())
            assert b'400' in reply, 'let through Content-Length %r' % value
            assert b'BODY_ON_GET' in reply
            # And the smuggled second request was not answered.
            assert reply.count(b'HTTP/1.') == 1
    finally:
        server.shutdown()
        server.server_close()


def test_a_zero_length_get_still_works():
    """Refusing a declared body must not refuse an ordinary request."""
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        request = 'GET /healthz HTTP/1.1\r\nHost: x\r\nContent-Length: 0\r\n\r\n'
        assert b'200' in _raw(cfg, request.encode())
    finally:
        server.shutdown()
        server.server_close()


def test_the_read_endpoints_compare_the_secret_in_constant_time():
    """The secret is in these paths too, not only the POST path."""
    import inspect

    from anybroker.webhook import server as server_module

    source = inspect.getsource(server_module)
    get_body = source[source.index('def do_GET') :]
    get_body = get_body[: get_body.index('class Server')]
    assert '_same(' in get_body
    assert "== secret_path" not in get_body
    assert "secret_path.rstrip('/') + '/status'" not in get_body


def test_the_read_endpoints_still_answer_and_still_refuse():
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
    server, _ = _serve(cfg)
    try:
        import http.client

        def get(path):
            conn = http.client.HTTPConnection('127.0.0.1', cfg.port, timeout=3)
            try:
                conn.request('GET', path)
                return conn.getresponse().status
            finally:
                conn.close()

        assert get(cfg.url_path + '/status') == 200
        assert get(cfg.url_path + '/journal') == 200
        assert get(cfg.url_path + '/refusals') == 200
        assert get('/healthz') == 200
        assert get(cfg.path + '/' + 'z' * len(SECRET) + '/status') == 404
        assert get(cfg.url_path) == 404
        assert get(cfg.url_path + '/nope') == 404
    finally:
        server.shutdown()
        server.server_close()


# ── credentials are not guessed from their names ───────────────────────────


def test_every_venue_option_is_sensitive_unless_obviously_public():
    """Naming the secret-looking keys misses whatever the next venue calls it.

    ``pwd``, ``auth``, ``jwt``, ``bearer``, ``signature`` and ``cookie`` all
    slip a substring test for 'password'/'secret'/'key', and every one of
    them is a credential.
    """
    secrets_by_name = {
        'pwd': 'hunter2hunter2',
        'auth': 'Bearer abc123xyz',
        'jwt': 'eyJhbGciOiJIUzI1',
        'bearer': 'zzzz9999',
        'signature': 'deadbeefcafe',
        'cookie': 'sid=abc12345',
        'passphrase': 'correct-horse',
        'totally_new_thing': 'a-value-nobody-anticipated',
    }
    cfg = config(venue_options=dict(secrets_by_name))
    values = cfg.sensitive_values()
    for name, value in secrets_by_name.items():
        assert value in values, '%s leaks' % name


def test_obviously_public_options_stay_readable():
    """Blanking a path would hide the useful half of "no file at ..."."""
    cfg = config(
        venue_options={
            'credential_path': '/Users/me/.tradovate_key',
            'token_dir': '/Users/me/.cache',
            'account': 'ACC1234',
            'host': '127.0.0.1',
            'port': 4002,
            'timeout': 20.0,
            'base_url': 'https://demo.tradovateapi.com/v1',
        }
    )
    values = cfg.sensitive_values()
    for public in (
        '/Users/me/.tradovate_key',
        '/Users/me/.cache',
        'ACC1234',
        '127.0.0.1',
        '4002',
        'https://demo.tradovateapi.com/v1',
    ):
        assert public not in values, '%s was blanked' % public


def test_a_booleans_value_is_not_treated_as_a_secret():
    """Redacting "True" would rewrite unrelated text."""
    cfg = config(venue_options={'readonly': True, 'retries': 3})
    assert 'True' not in cfg.sensitive_values()
    assert '3' not in cfg.sensitive_values()


def test_an_unnamed_credential_in_an_error_is_cleaned():
    cfg = config(venue_options={'pwd': 'hunter2hunter2'}, journal=None)

    class LeakyVenue(FakeVenue):
        def list_positions(self):
            raise RuntimeError('login failed for pwd=hunter2hunter2')

    out = Router(cfg, venue=LeakyVenue()).status()
    assert 'hunter2hunter2' not in json.dumps(out)


# ── no raw exception text escapes ──────────────────────────────────────────


def test_a_path_named_option_is_public_only_if_it_holds_a_path():
    """``credential_path`` names a file; ``token_file`` may hold a token."""
    cfg = config(
        venue_options={
            'credential_path': '/Users/me/.tradovate_key',
            'token_dir': '~/.cache/tv',
            'token_file': 'abc123notapath',
        }
    )
    values = cfg.sensitive_values()
    assert '/Users/me/.tradovate_key' not in values
    assert '~/.cache/tv' not in values
    assert 'abc123notapath' in values, 'a token called a file still leaked'


def test_a_public_word_in_a_credential_name_does_not_make_it_public():
    """Whole words, not substrings: 'port' must not excuse 'passport'."""
    leaks = {
        'secret_env': 'leaked-secret-one',
        'api_key_url': 'leaked-key-two',
        'passport': 'leaked-three',
        'auth_host': 'leaked-four',
    }
    cfg = config(venue_options=dict(leaks))
    values = cfg.sensitive_values()
    for name, value in leaks.items():
        assert value in values, '%s leaks' % name


def test_credentials_inside_a_public_url_are_stripped():
    cfg = config(
        venue_options={
            'base_url': 'https://user:hunter2pass@feed.example.com/v1?apikey=zzzz9999'
        }
    )
    values = cfg.sensitive_values()
    assert 'hunter2pass' in values
    assert 'zzzz9999' in values


def test_the_venue_open_failure_is_cleaned():
    """This message reaches the journal and the terminal."""
    cfg = config(venue='nope', venue_options={'pwd': 'hunter2hunter2'})
    r = Router(cfg)
    with pytest.raises(RouterError) as caught:
        r.connect()
    assert 'hunter2hunter2' not in caught.value.message


def test_no_raw_exception_text_is_returned_or_printed():
    """A source guard: every escape route goes through the cleaner.

    Checked at the source because the alternative is remembering, every
    time a handler is added, that the broker's words have seen the keys.
    """
    import inspect

    from anybroker.webhook import router as router_module
    from anybroker.webhook import server as server_module

    for module in (router_module, server_module):
        source = inspect.getsource(module)
        for bad in ("= str(exc)", "% exc,", "% exc)", "{exc}"):
            assert bad not in source, '%s still has %r' % (module.__name__, bad)


# ── a size has to be a real number ─────────────────────────────────────────


def test_nan_and_infinity_are_refused_as_sizes():
    """`float('nan') <= 0` is False, so NaN clears a "must be positive" test.

    Python's json module accepts the bare literals, so an alert can just
    say NaN and an order of that size would be built.
    """
    for literal in ('NaN', 'Infinity', '-Infinity', '"nan"', '"inf"', '"-inf"'):
        body = '{"action":"buy","symbol":"ABCZ6","qty":%s}' % literal
        with pytest.raises(AlertError) as caught:
            parse_alert(body)
        assert caught.value.code == 'BAD_NUMBER', literal


def test_a_non_finite_price_or_stop_is_refused():
    for field in ('price', 'stop', 'take_profit'):
        body = '{"action":"buy","symbol":"ABCZ6","qty":1,"%s":Infinity}' % field
        with pytest.raises(AlertError) as caught:
            parse_alert(body)
        assert caught.value.code == 'BAD_NUMBER', field


def test_ordinary_numbers_still_pass():
    alert = parse_alert('{"action":"buy","symbol":"ABCZ6","qty":2,"price":20150.25}')
    assert alert.qty == 2
    assert alert.price == 20150.25


# ── an escaped secret is still the secret ──────────────────────────────────


def test_a_secret_containing_a_quote_is_not_left_escaped():
    """A quote becomes \\" inside JSON, which a literal replace misses."""
    from anybroker.webhook import redact_secret

    secret = 'abc"def-ghijklmnopqrstuvwxyz12345'
    out = redact_secret(json.dumps({'secret': secret, 'note': secret}), secret)
    assert secret not in out
    assert json.dumps(secret)[1:-1] not in out


def test_a_non_ascii_secret_is_not_left_escaped():
    """json.dumps escapes non-ASCII to \\uXXXX by default."""
    from anybroker.webhook import redact_secret

    secret = 'pässwörd-ünicode-secret-123456'
    out = redact_secret(json.dumps({'secret': secret, 'note': secret}), secret)
    assert secret not in out
    assert 'p\\u00e4ssw' not in out
    assert secret.encode('unicode_escape').decode() not in out


def test_a_url_encoded_secret_is_redacted():
    """It travels in a URL, so it can come back percent-encoded."""
    import urllib.parse

    from anybroker.webhook import redact_secret

    secret = 'abc/def+ghi-jklmnopqrstuvwxyz12'
    text = 'path was /alert/%s' % urllib.parse.quote(secret, safe='')
    assert secret not in redact_secret(text, secret)
    assert urllib.parse.quote(secret, safe='') not in redact_secret(text, secret)


# ── a slow body cannot hold a thread forever ───────────────────────────────


def test_a_trickled_body_is_cut_off_by_the_total_deadline():
    """The socket timeout is per-recv, so a byte at a time resets it forever.

    Patched short here rather than waiting the real deadline out.
    """
    import anybroker.webhook.server as server_module

    original = server_module.READ_TIMEOUT
    server_module.READ_TIMEOUT = 0.4
    try:
        cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
        server, _ = _serve(cfg)
        try:
            sock = socket.create_connection(('127.0.0.1', cfg.port), timeout=5)
            try:
                sock.sendall(
                    (
                        'POST %s HTTP/1.1\r\nHost: x\r\nContent-Length: 40\r\n\r\n'
                        % cfg.url_path
                    ).encode()
                )
                sock.sendall(b'{')  # one byte, then stall
                started = time.time()
                reply = sock.recv(65536)
                assert b'408' in reply or b'BODY_TOO_SLOW' in reply, reply[:80]
                assert time.time() - started < 4
            finally:
                sock.close()
        finally:
            server.shutdown()
            server.server_close()
    finally:
        server_module.READ_TIMEOUT = original


def test_a_body_arriving_in_pieces_but_in_time_is_still_accepted():
    """The deadline must not punish a merely slow sender.

    The read now takes what has arrived rather than waiting for the whole
    declared length, and narrows the socket timeout to the remaining
    budget before each piece. Both of those could drop or truncate a body
    that arrives in two packets -- which is ordinary on a real network --
    so this sends one that way, inside the budget, and expects it whole.
    """
    cfg = config(port=_free_port(), allow_any_ip=True, journal=None, dry_run=True)
    server, _ = _serve(cfg)
    try:
        payload = b'{"action":"buy","symbol":"ABCZ6","qty":1}'
        sock = socket.create_connection(('127.0.0.1', cfg.port), timeout=5)
        try:
            sock.sendall(
                (
                    'POST %s HTTP/1.1\r\nHost: x\r\n'
                    'Content-Type: application/json\r\n'
                    'Content-Length: %d\r\n\r\n' % (cfg.url_path, len(payload))
                ).encode()
            )
            # Three pieces, with a pause between them, well inside the budget.
            for cut in (0, 12, 25):
                end = {0: 12, 12: 25, 25: len(payload)}[cut]
                sock.sendall(payload[cut:end])
                time.sleep(0.05)
            reply = sock.recv(65536)
            assert b'202' in reply, reply[:120]
        finally:
            sock.close()
    finally:
        server.shutdown()
        server.server_close()


def test_there_is_a_cap_on_requests_in_flight():
    from anybroker.webhook.server import MAX_IN_FLIGHT, make_server

    assert 1 <= MAX_IN_FLIGHT <= 1024
    cfg = config(port=_free_port(), journal=None)
    server, _ = make_server(cfg)
    try:
        assert hasattr(server, '_slots')
        # Exhaust it, then confirm a further acquire is refused rather than
        # queued -- which is what keeps a stall from taking every thread.
        taken = [server._slots.acquire(blocking=False) for _ in range(MAX_IN_FLIGHT)]
        assert all(taken)
        assert server._slots.acquire(blocking=False) is False
        for _ in taken:
            server._slots.release()
    finally:
        server.server_close()


# ── headers cannot be trickled either ──────────────────────────────────────


def test_trickled_headers_do_not_hold_a_slot():
    """Header bytes that can never finish the header, sent forever.

    This is the case a socket timeout cannot see: every recv succeeds, so it
    never fires, while the request line and headers are read by the base
    class before any of this module's code runs. The bytes are all ``X`` --
    never CR or LF -- so the headers cannot complete by accident and the
    connection can only end because something ended it.
    """
    import anybroker.webhook.server as server_module

    original = server_module.READ_TIMEOUT
    server_module.READ_TIMEOUT = 0.6
    try:
        cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
        server, _ = _serve(cfg)
        try:
            sock = socket.create_connection(('127.0.0.1', cfg.port), timeout=10)
            try:
                sock.sendall(
                    ('POST %s HTTP/1.1\r\nHost: x\r\nX-Pad: ' % cfg.url_path).encode()
                )
                started = time.time()
                dropped = False
                while time.time() - started < 2.5:
                    try:
                        sock.sendall(b'X')
                    except OSError:
                        dropped = True
                        break
                    time.sleep(0.05)
                # Asserted *while still sending*. A per-recv timeout only
                # fires once the trickle stops, so waiting for silence would
                # pass either way and prove nothing.
                assert dropped, (
                    'the connection survived %.1fs of continuous trickled '
                    'headers' % (time.time() - started)
                )
            finally:
                sock.close()
            assert _post(cfg)[0] == 202
        finally:
            server.shutdown()
            server.server_close()
    finally:
        server_module.READ_TIMEOUT = original


def test_an_idle_keep_alive_connection_is_dropped():
    """A connection that sends one request and then sits there.

    A guard, not a regression test: a connection sending *nothing* was
    already closed by the per-recv socket timeout, and this passes with or
    without the per-request deadline. It is here so the behaviour cannot be
    lost -- the case the deadline actually added is the trickle above.
    """
    import anybroker.webhook.server as server_module

    original = server_module.READ_TIMEOUT
    server_module.READ_TIMEOUT = 0.5
    try:
        cfg = config(port=_free_port(), allow_any_ip=True, journal=None)
        server, _ = _serve(cfg)
        try:
            sock = socket.create_connection(('127.0.0.1', cfg.port), timeout=6)
            try:
                sock.sendall(b'GET /healthz HTTP/1.1\r\nHost: x\r\n\r\n')
                assert b'200' in sock.recv(4096)
                started = time.time()
                sock.settimeout(5)
                try:
                    sock.recv(4096)
                except OSError:
                    pass
                assert time.time() - started < 4.5, 'idle connection kept open'
            finally:
                sock.close()
        finally:
            server.shutdown()
            server.server_close()
    finally:
        server_module.READ_TIMEOUT = original


# ── a secret with a slash in front of it is not a path ─────────────────────


def test_a_slash_prefixed_secret_is_not_published_as_a_path():
    """``/aGVsbG8=`` is base64 with a slash, not a filesystem path."""
    cfg = config(
        venue_options={
            'secret_file': '/aGVsbG8gd29ybGQ=',
            'api_key_path': '/AKIAEXAMPLEKEY+abc=',
        }
    )
    values = cfg.sensitive_values()
    assert '/aGVsbG8gd29ybGQ=' in values
    assert '/AKIAEXAMPLEKEY+abc=' in values


def test_real_paths_are_still_published():
    cfg = config(
        venue_options={
            'credential_path': '/Users/me/.tradovate_key',
            'token_dir': '~/.cache/tv',
            'extra_file': './local/key.json',
        }
    )
    values = cfg.sensitive_values()
    for path in ('/Users/me/.tradovate_key', '~/.cache/tv', './local/key.json'):
        assert path not in values, '%s was blanked' % path


def test_a_key_used_as_a_url_username_is_redacted():
    """Plenty of services put the key in the username half of userinfo."""
    cfg = config(
        venue_options={'base_url': 'https://AKIAKEYASUSER:pw1234@h.example.com/v1'}
    )
    values = cfg.sensitive_values()
    assert 'AKIAKEYASUSER' in values
    assert 'pw1234' in values
