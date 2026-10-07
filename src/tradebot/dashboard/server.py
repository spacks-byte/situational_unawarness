"""Local research dashboard. Supabase is read-only; no exchange trading client is used."""
from __future__ import annotations

import argparse
import hmac
import json
import secrets
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pydantic import ValidationError

from tradebot.core.config import Settings
from tradebot.dashboard.remote import BinanceData, DataError, SupabaseLedger, live_payload, normalize_order
from tradebot.dashboard.research import BacktestRequest, clean, live_executions, perform_backtest
from tradebot.strategy.library.rxm import UNIVERSE


class Dashboard:
    def __init__(self, settings=None, ledger=None, market=None):
        settings = settings or Settings.load()
        self.ledger = ledger or SupabaseLedger()
        self.market = market or BinanceData(settings.data.dir)
        self.csrf = secrets.token_urlsafe(32)
        self.jobs = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='dashboard-backtest')

    def submit(self, payload):
        request = BacktestRequest.model_validate(payload)
        with self.lock:
            if any(job['status'] in {'queued', 'running'} for job in self.jobs.values()):
                raise DataError('A backtest is already running. Wait for it to finish before starting another.')
            while len(self.jobs) >= 5:
                del self.jobs[next(iter(self.jobs))]
            job_id = uuid.uuid4().hex
            self.jobs[job_id] = dict(id=job_id, status='queued', progress='Queued')
        self.executor.submit(self._backtest, job_id, request)
        return {'id': job_id, 'status': 'queued'}

    def _backtest(self, job_id, request):
        def update(message):
            with self.lock:
                self.jobs[job_id].update(status='running', progress=message)
        try:
            update('Loading Binance candles')
            result, exports = perform_backtest(request, self.market, update)
            with self.lock:
                self.jobs[job_id].update(status='complete', progress='Complete', result=result, exports=exports)
        except (DataError, ValueError) as exc:
            with self.lock:
                self.jobs[job_id].update(status='failed', error=str(exc))
        except Exception:
            # Do not expose request headers, environment, or provider response bodies.
            with self.lock:
                self.jobs[job_id].update(status='failed', error='Backtest failed unexpectedly. Check candle data and strategy configuration.')

    def job(self, job_id):
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError('Run not found or expired. The five most recent runs are retained until restart.')
            return {k:v for k,v in self.jobs[job_id].items() if k != 'exports'}


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = 'TradingDesk/1.0'

    @property
    def app(self):
        return self.server.app

    def log_message(self, *_):
        pass

    def send(self, data, status=200, content_type='application/json; charset=utf-8', filename=None):
        if content_type.startswith('application/json'):
            data = json.dumps(clean(data), allow_nan=False).encode()
        elif isinstance(data, str):
            data = data.encode()
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header('Content-Disposition', f'attachment; filename="{filename}"')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def allowed(self):
        host = self.headers.get('Host', '')
        allowed = {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}
        origin = self.headers.get('Origin')
        return host in allowed and (not origin or origin in {'http://' + h for h in allowed})

    def do_GET(self):
        if not self.allowed():
            return self.send({'error':'Local dashboard access only.'}, 403)
        parsed = urlparse(self.path)
        path = parsed.path
        query = {k:v[0] for k,v in parse_qs(parsed.query).items()}
        try:
            assets = {'/': ('app.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/app.css': ('app.css', 'text/css; charset=utf-8')}
            if path in assets:
                name, kind = assets[path]
                return self.send(Path(__file__).with_name(name).read_bytes(), content_type=kind)
            if path == '/api/config':
                return self.send(dict(csrf=self.app.csrf, strategies=['rxm', 'ma_crossover'], rxm_symbols=UNIVERSE,
                                      defaults=['BTC', 'ETH', 'SOL', 'BNB', 'XRP']))
            if path == '/api/live':
                return self.send(live_payload(self.app.ledger, self.app.market))
            if path == '/api/executions':
                orders = [normalize_order(r) for r in self.app.ledger.rows()]
                return self.send(live_executions(orders, self.app.market, query.get('strategy'), query.get('symbol', ''), query.get('bot')))
            if path.startswith('/api/backtests/'):
                parts = path.strip('/').split('/')
                job_id = parts[2]
                if len(parts) == 3:
                    return self.send(self.app.job(job_id))
                if len(parts) == 4 and parts[3] in {'quotes.csv', 'trades.csv'}:
                    with self.app.lock:
                        job = self.app.jobs.get(job_id)
                        if not job or job['status'] != 'complete':
                            raise KeyError('Export unavailable. Run the backtest again.')
                        csv = job['exports'][parts[3][:-4]]
                    return self.send(csv, content_type='text/csv; charset=utf-8', filename=f'{job_id[:8]}-{parts[3]}')
            self.send({'error':'Not found'}, 404)
        except KeyError as exc:
            self.send({'error':str(exc)}, 404)
        except ValueError as exc:
            self.send({'error':str(exc)}, 400)
        except DataError as exc:
            self.send({'error':str(exc)}, 502)
        except Exception:
            self.send({'error':'Unable to load dashboard data. Retry shortly.'}, 500)

    def do_POST(self):
        if not self.allowed() or not hmac.compare_digest(self.headers.get('X-Dashboard-Token', ''), self.app.csrf):
            return self.send({'error':'Refresh the dashboard before submitting a run.'}, 403)
        if self.path != '/api/backtests':
            return self.send({'error':'Not found'}, 404)
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if not 0 < size <= 32_000:
                return self.send({'error':'Invalid request size'}, 413)
            if not self.headers.get('Content-Type', '').startswith('application/json'):
                return self.send({'error':'Expected JSON'}, 415)
            request = json.loads(self.rfile.read(size))
            self.send(self.app.submit(request), 202)
        except ValidationError as exc:
            errors = ['.'.join(str(x) for x in e['loc']) + ': ' + e['msg'] for e in exc.errors(include_input=False, include_url=False)]
            self.send({'error':'; '.join(errors)}, 400)
        except (ValueError, TypeError):
            self.send({'error':'Invalid JSON request'}, 400)
        except DataError as exc:
            self.send({'error':str(exc)}, 409)


def make_server(port=8765, app=None):
    server = ThreadingHTTPServer(('127.0.0.1', port), DashboardHandler)
    server.app = app or Dashboard()
    return server


def main(argv=None, settings=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    server = make_server(args.port, Dashboard(settings))
    print(f'Trading dashboard: http://127.0.0.1:{server.server_port}', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        server.app.executor.shutdown(wait=False, cancel_futures=True)
    return 0


if __name__ == '__main__':
    main()
