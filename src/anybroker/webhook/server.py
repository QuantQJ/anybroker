"""The HTTP endpoint TradingView posts to.

Standard library only -- no web framework, because the whole job is: check the
sender, check the secret, read one small body, and answer.

The three-second rule shapes the design
---------------------------------------
TradingView cancels the request after three seconds, so the reply cannot wait
on a broker. The handler validates, writes the alert down, answers **202
Accepted**, and places the order on a worker thread.

That means a 202 says *received and accepted for routing*, never *filled*.
Nothing truthful could say more in three seconds, and a bridge that blocked on
the broker would have TradingView hang up mid-order and leave the fate of that
order unknown. The journal is the record; the HTTP status is a receipt.

Two things are checked before anything else, in this order: the sender's
address, then the secret. A request failing either is answered 404 -- not 401
or 403 -- so a scanner cannot tell a wrong secret from a wrong path.
"""

from __future__ import annotations

import hmac
import json
import queue
import re
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .alert import AlertError, parse_alert, redact_secret
from .config import Config
from .journal import Journal
from .router import RouterError

__all__ = ['Bridge', 'make_server']

#: The largest body worth reading. A TradingView alert is a line or two.
MAX_BODY = 64 * 1024

#: How long one request may take to arrive, in total. The socket timeout
#: alone is per-recv: a client sending one byte every fourteen seconds
#: would never trip it and would hold its thread indefinitely. So the body
#: read is also bounded by a wall-clock deadline. TradingView gives up after
#: three seconds, so this is generous.
READ_TIMEOUT = 15.0

#: How many requests may be in flight at once. Threads are cheap but not
#: free, and the point of a cap is that a stalled connection cannot be
#: multiplied into every thread the machine will give.
MAX_IN_FLIGHT = 64


def _same(left, right) -> bool:
    """Constant-time string comparison, whatever the strings contain."""
    return hmac.compare_digest(
        str(left).encode('utf-8', 'replace'), str(right).encode('utf-8', 'replace')
    )


class Bridge:
    """Ties the config, the journal, the router and the worker together."""

    def __init__(self, config: Config, *, router=None, journal=None) -> None:
        self.config = config
        self.journal = journal or Journal(
            config.journal, secrets=config.sensitive_values()
        )
        if router is None:
            from .router import Router

            router = Router(config, journal=self.journal)
        self.router = router
        self._queue: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.placed = 0
        self.refused = 0
        #: Redeliveries that anybroker refused to turn into a second order.
        #: Counted apart from ``placed`` so the record cannot overstate how
        #: many orders were actually sent.
        self.duplicates = 0

    # ── lifecycle ───────────────────────────────────────────────────────────

    def start(self, *, connect=True) -> Bridge:
        if connect:
            self.router.connect()
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._drain, name='anyhook-worker', daemon=True
        )
        self._worker.start()
        return self

    def stop(self, *, timeout=5.0) -> None:
        self._stop.set()
        self._queue.put(None)
        if self._worker is not None:
            self._worker.join(timeout)
        self.router.close()

    # ── the hot path ────────────────────────────────────────────────────────

    def accept(self, body, *, source_ip=None) -> tuple:
        """Validate and enqueue one alert. Returns ``(status, payload)``.

        Fast by design: no network, no broker. Everything slow happens on the
        worker.
        """
        try:
            alert = parse_alert(
                body,
                default_venue=self.config.venue,
                default_qty=self.config.default_qty,
            )
        except AlertError as exc:
            self.refused += 1
            # The message quotes the value it rejected, and that value may be
            # the secret -- an alert with the secret in the wrong field would
            # otherwise write it into the journal and echo it back.
            detail = redact_secret(exc.message, self.config.secret)
            self.journal.record(
                'refused',
                code=exc.code,
                detail=detail,
                venue=self.config.venue,
                env=self.config.env,
                source_ip=source_ip,
                body=redact_secret(body, self.config.secret),
            )
            return 400, {'status': 'refused', 'code': exc.code, 'detail': detail}

        self.journal.record(
            'accepted',
            alert=alert,
            venue=self.config.venue,
            env=self.config.env,
            source_ip=source_ip,
            body=redact_secret(body, self.config.secret),
        )
        self._queue.put(alert)
        return 202, {
            'status': 'accepted',
            'order_id': alert.order_id,
            'action': alert.action,
            'symbol': alert.symbol,
            'qty': alert.qty,
            'note': 'accepted for routing; see the journal for the outcome',
        }

    def _drain(self) -> None:
        while not self._stop.is_set():
            alert = self._queue.get()
            if alert is None:
                return
            self._place(alert)

    def _place(self, alert) -> None:
        try:
            result = self.router.place(alert)
        except RouterError as exc:
            self.refused += 1
            self.journal.record(
                'unresolved' if exc.ambiguous else 'failed',
                alert=alert,
                code=exc.code,
                detail=exc.message,
                venue=self.config.venue,
                env=self.config.env,
            )
            return
        except Exception as exc:  # never kill the worker
            self.refused += 1
            self.journal.record(
                'failed',
                alert=alert,
                code='UNEXPECTED',
                detail=self.router._safe(exc)
                if hasattr(self.router, '_safe')
                else type(exc).__name__,
                venue=self.config.venue,
                env=self.config.env,
            )
            return
        if result.get('outcome') == 'duplicate':
            self.duplicates += 1
        else:
            self.placed += 1
        self.journal.record(
            result.get('outcome', 'placed'),
            alert=alert,
            detail=result.get('detail'),
            broker_id=result.get('broker_id'),
            venue=self.config.venue,
            env=self.config.env,
        )

    def drain_now(self, *, timeout=5.0) -> None:
        """Wait for the queue to empty. For tests and for a clean shutdown."""
        deadline = time.time() + timeout
        while time.time() < deadline and not self._queue.empty():
            time.sleep(0.005)

    # ── read-only ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        out = {
            'venue': self.config.venue,
            'env': self.config.env,
            'dry_run': self.config.dry_run,
            'accepted': self.placed + self.refused + self.duplicates,
            'placed': self.placed,
            'refused': self.refused,
            'duplicates': self.duplicates,
            'queued': self._queue.qsize(),
            'journal': self.journal.counts(),
        }
        try:
            out['desk'] = self.router.status()
        except Exception as exc:
            # This is returned over HTTP, so it goes out cleaned.
            cleaner = getattr(self.router, '_safe', None)
            out['desk_error'] = cleaner(exc) if cleaner else type(exc).__name__
        return out


def make_server(config: Config, *, bridge=None) -> tuple:
    """Build the HTTP server. Returns ``(server, bridge)``."""
    bridge = bridge or Bridge(config)
    secret_path = config.url_path

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        timeout = READ_TIMEOUT

        def handle_one_request(self) -> None:
            """One request, with a deadline over the whole of it.

            The body already had one, but the request line and the headers
            are read by the base class before any of this module's code
            runs -- so a client trickling *headers* a byte at a time was
            bounded by nothing, and held its slot for as long as it liked.
            The same timer covers an idle keep-alive connection, which is
            the other way to hold a slot without sending anything.
            """
            # One deadline, shared. _read_body used to set its own, a full
            # READ_TIMEOUT from the moment it was reached -- which is always
            # *later* than this one, so the hard cutoff below always won the
            # race and the connection died without the 408 ever being sent.
            self._deadline = time.monotonic() + READ_TIMEOUT
            cutoff = threading.Timer(READ_TIMEOUT, self._cut_off)
            cutoff.daemon = True
            cutoff.start()
            try:
                super().handle_one_request()
            finally:
                cutoff.cancel()

        def _reply_by(self) -> float:
            """When a slow read must give up, early enough to still answer.

            A hair before the hard cutoff, so the 408 is written while the
            socket is still open. Proportional, so a short READ_TIMEOUT in a
            test does not leave a negative budget.
            """
            deadline = getattr(self, '_deadline', None)
            if deadline is None:
                deadline = time.monotonic() + READ_TIMEOUT
            return deadline - min(0.5, READ_TIMEOUT * 0.1)

        def _cut_off(self) -> None:
            """Stop waiting. Closing the socket is what unblocks the read."""
            self.close_connection = True
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        server_version = 'anyhook'
        sys_version = ''

        def log_message(self, fmt, *args):
            """Quiet by default: the path contains the secret."""

        # ── guards, cheapest first ──────────────────────────────────────────

        def _is_loopback(self) -> bool:
            return (self.client_address or ('',))[0] in ('127.0.0.1', '::1')

        def _read_body(self, length):
            """The declared body, or None if it did not arrive in time.

            Read against a wall-clock deadline, in pieces, with the socket
            timeout narrowed to what is left of the budget before each one.

            Two things make the obvious version not work. The socket timeout
            is per-recv, so a client trickling a byte at a time resets it
            forever. And ``rfile.read(n)`` does not come back until it has
            all *n* bytes -- so a deadline checked between reads is never
            reached at all while one read sits there waiting. ``read1``
            returns what has arrived, which is what lets the loop get back
            round to the check.

            The point of answering rather than being cut off: a 408 tells
            the sender its request was too slow. A dropped connection looks
            like a crash.
            """
            if not length:
                return b''
            deadline = self._reply_by()
            chunks = []
            remaining = length
            original_timeout = self.connection.gettimeout()
            try:
                while remaining > 0:
                    budget = deadline - time.monotonic()
                    if budget <= 0:
                        return None
                    try:
                        self.connection.settimeout(budget)
                        chunk = self.rfile.read1(min(remaining, 8192))
                    except (TimeoutError, OSError):
                        return None
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
            finally:
                try:
                    self.connection.settimeout(original_timeout)
                except OSError:
                    pass
            if remaining > 0:
                return None
            return b''.join(chunks)

        def _declared_length(self):
            """How many bytes the request says it carries, or None if it lied.

            One header, plain ASCII digits, nothing else. Two Content-Length
            headers are a smuggling primitive on their own: ``get`` returns
            the first, while something downstream may believe the second.
            """
            values = self.headers.get_all('Content-Length') or []
            if not values:
                return 0
            if len(values) > 1 and len({str(v).strip() for v in values}) > 1:
                return None
            raw = str(values[0]).strip()
            if not re.fullmatch(r'[0-9]+', raw):
                return None
            return int(raw)

        def _client_ip(self) -> str:
            """Who is actually asking.

            Normally the peer. Behind a declared proxy, the rightmost entry
            of ``X-Forwarded-For`` -- the one that proxy appended, which is
            the address it really saw. Earlier entries are whatever the
            client chose to claim, so they are ignored.

            The header is read only when the peer is in ``trusted_proxies``.
            Trusting it otherwise would let any sender name its own address.
            """
            peer = (self.client_address or ('',))[0]
            if peer not in config.trusted_proxies:
                return peer
            # Every line of the one header the operator named. A client can
            # send its own copy, and ``get`` would return that one rather
            # than the proxy's -- so all lines are joined and the rightmost
            # entry wins, which is the hop the proxy itself appended.
            lines = self.headers.get_all(config.forwarded_header) or []
            entries = [
                part.strip()
                for line in lines
                for part in str(line).split(',')
                if part.strip()
            ]
            return entries[-1] if entries else peer

        def _sender_allowed(self) -> bool:
            if config.allow_any_ip:
                return True
            return self._client_ip() in config.allow_ips

        def _authorised(self, body_secret=None) -> bool:
            # compare_digest rather than ==, which stops comparing at the
            # first wrong character. The margin is tiny next to network
            # jitter, but the constant-time compare costs nothing.
            if _same(self.path.rstrip('/'), secret_path.rstrip('/')):
                return True
            # The secret may also travel in the payload, which keeps it out
            # of access logs and proxy history.
            if body_secret and _same(str(body_secret), config.secret):
                return self.path.rstrip('/') == config.path.rstrip('/')
            return False

        def _refuse(self, status, payload) -> None:
            """Answer and hang up.

            This is HTTP/1.1, so the connection is kept alive by default. A
            refusal that answers without reading the body leaves those bytes
            in the pipe, where they are parsed as the *next* request on the
            same connection -- which is a request smuggled past the sender
            and secret checks. Closing ends that.
            """
            self.close_connection = True
            payload = dict(payload)
            self._reply(status, payload, extra_headers=(('Connection', 'close'),))

        def _reply(self, status, payload, *, extra_headers=()) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            for name, value in extra_headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _not_found(self) -> None:
            # 404 for a bad sender, a bad secret and a bad path alike, so a
            # scanner learns nothing from the difference.
            self._refuse(404, {'status': 'not found'})

        # ── the endpoints ──────────────────────────────────────────────────

        def do_POST(self) -> None:
            # A chunked body has no Content-Length, so nothing below would
            # read it and it would arrive as the next request on this
            # connection. TradingView does not send one; refuse it rather
            # than half-support it.
            if self.headers.get('Transfer-Encoding'):
                self._refuse(400, {'status': 'refused', 'code': 'TRANSFER_ENCODING'})
                return
            if not self._sender_allowed():
                bridge.journal.record(
                    'rejected_sender',
                    code='BAD_SOURCE_IP',
                    detail=(self.client_address or ('',))[0],
                    venue=config.venue,
                    env=config.env,
                    source_ip=self._client_ip(),
                )
                self._not_found()
                return
            length = self._declared_length()
            # Malformed, not large: anything but plain digits is refused
            # rather than coerced. int() would read '5_0' as 50 and ' 5 ' as
            # 5, so the number parsed here would not be the number of bytes
            # on the socket -- and the remainder becomes the next request.
            if length is None:
                self._refuse(400, {'status': 'refused', 'code': 'BAD_LENGTH'})
                return
            if length > MAX_BODY:
                self._refuse(413, {'status': 'refused', 'code': 'TOO_LARGE'})
                return
            raw = self._read_body(length)
            if raw is None:
                self._refuse(408, {'status': 'refused', 'code': 'BODY_TOO_SLOW'})
                return
            body = raw.decode('utf-8', 'replace')

            payload_secret = None
            if body[:1] == '{':
                try:
                    payload_secret = (json.loads(body) or {}).get('secret')
                except ValueError:
                    payload_secret = None
            if not self._authorised(payload_secret):
                bridge.journal.record(
                    'rejected_secret',
                    code='BAD_SECRET',
                    venue=config.venue,
                    env=config.env,
                    source_ip=self._client_ip(),
                )
                self._not_found()
                return

            status, payload = bridge.accept(body, source_ip=self._client_ip())
            self._reply(status, payload)

        def do_GET(self) -> None:
            """Status and the journal, behind the same secret."""
            # Nothing here reads a body, so a GET that declares one would
            # leave those bytes to be read as the next request. None of
            # these endpoints takes input; refuse and hang up.
            #
            # Tested against None explicitly: a malformed length is reported
            # as None, which is falsy, so a truthiness test would read
            # "Content-Length: 5_0" as no body at all and let it through --
            # the one case this check exists for.
            length = self._declared_length()
            if self.headers.get('Transfer-Encoding') or length is None or length > 0:
                self._refuse(400, {'status': 'refused', 'code': 'BODY_ON_GET'})
                return
            path = self.path.rstrip('/')
            # Liveness answers the machine itself whatever the allowlist says,
            # so a launchd or systemd probe does not need the secret or a
            # place on the list. It reveals nothing but that the port is up.
            if path == '/healthz' and self._is_loopback():
                self._reply(200, {'status': 'ok'})
                return
            if not self._sender_allowed() and not config.allow_any_ip:
                self._not_found()
                return
            # The secret is in this path too, so the prefix is matched in
            # constant time and only the suffix is dispatched on.
            prefix = secret_path.rstrip('/')
            if len(path) > len(prefix) and _same(path[: len(prefix)], prefix):
                suffix = path[len(prefix) :]
                if suffix == '/status':
                    self._reply(200, bridge.status())
                    return
                if suffix == '/journal':
                    self._reply(
                        200,
                        {
                            'counts': bridge.journal.counts(),
                            'recent': bridge.journal.recent(50),
                        },
                    )
                    return
                if suffix == '/refusals':
                    self._reply(200, {'refusals': bridge.journal.refusals(50)})
                    return
            if path == '/healthz':
                self._reply(200, {'status': 'ok'})
                return
            self._not_found()

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def __init__(self, *args, **kw) -> None:
            self._slots = threading.BoundedSemaphore(MAX_IN_FLIGHT)
            super().__init__(*args, **kw)

        def process_request(self, request, client_address) -> None:
            """Refuse a connection rather than start a thread for it.

            Acquired here and released in process_request_thread, so the
            count follows the thread that does the work.
            """
            if not self._slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try:
                super().process_request(request, client_address)
            except BaseException:
                self._slots.release()
                raise

        def process_request_thread(self, request, client_address) -> None:
            try:
                super().process_request_thread(request, client_address)
            finally:
                self._slots.release()

        def handle_error(self, request, client_address) -> None:
            """A disconnected client is expected here, not a crash.

            TradingView cancels the request after three seconds, so a client
            vanishing mid-reply is ordinary traffic. The default handler
            prints a traceback for it, which fills a service log with alarm
            about nothing. Anything else is still reported.
            """
            exc = sys.exc_info()[1]
            if isinstance(exc, (ConnectionError, BrokenPipeError, TimeoutError)):
                return
            super().handle_error(request, client_address)

    server = Server((config.host, config.port), Handler)
    return server, bridge
