"""Authenticated PAPER-only analytics and predefined operational controls."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
from dataclasses import asdict
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .analytics import build_analytics
from .config import load_settings
from .service import run_analysis_cycle
from .state import AuditStore

MAX_BODY_BYTES = 4096


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    return value


def _read_token(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("dashboard auth token must be a regular non-symlink file")
    if path.stat().st_size > 4096:
        raise ValueError("dashboard auth token file is too large")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < 24:
        raise ValueError("dashboard auth token must contain at least 24 characters")
    return token


class DashboardApplication:
    def __init__(self, config_path: Path, db_path: Path, token: str) -> None:
        self.config_path = config_path.resolve()
        self.db_path = db_path.resolve()
        self.token = token
        self.csrf_token = hmac.new(token.encode(), b"paper-dashboard-csrf-v1", hashlib.sha256).hexdigest()
        self._action_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._job: dict[str, Any] = {"running": False, "action": None, "result": None, "error": None}
        load_settings(self.config_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def authorized(self, header: str | None) -> bool:
        if not header or not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (ValueError, UnicodeError):
            return False
        return hmac.compare_digest(username, "admin") and hmac.compare_digest(password, self.token)

    def job_status(self) -> dict[str, Any]:
        with self._job_lock:
            return dict(self._job)

    def start_cycle(self, action: str) -> bool:
        if action not in {"observe", "paper"} or not self._action_lock.acquire(blocking=False):
            return False
        with self._job_lock:
            self._job = {"running": True, "action": action, "result": None, "error": None}

        def worker() -> None:
            try:
                result = run_analysis_cycle(self.config_path, self.db_path, paper=action == "paper")
                summary = {
                    "mode": result.get("mode"),
                    "generated_at_ms": result.get("generated_at_ms"),
                    "equity_usdt": result.get("equity_usdt"),
                    "daily_pnl_usdt": result.get("daily_pnl_usdt"),
                    "open_positions": len(result.get("open_positions", [])),
                    "entry_candidates": result.get("entry_candidates", []),
                }
                with self._job_lock:
                    self._job = {"running": False, "action": action, "result": summary, "error": None}
            except Exception as exc:  # Safe message types are normalized by service layers.
                with self._job_lock:
                    self._job = {"running": False, "action": action, "result": None, "error": str(exc)[:500]}
            finally:
                self._action_lock.release()

        threading.Thread(target=worker, name=f"dashboard-{action}", daemon=True).start()
        return True

    def set_paused(self, paused: bool, reason: str | None) -> dict[str, Any]:
        store = AuditStore(self.db_path)
        try:
            store.set_paper_paused(paused, reason)
            return store.paper_control()
        finally:
            store.close()

    def status(self) -> dict[str, Any]:
        store = AuditStore(self.db_path)
        try:
            latest = store.latest_paper_report() or store.latest_report()
            return {
                "job": self.job_status(),
                "control": store.paper_control(),
                "latest": latest,
                "orders_enabled": False,
            }
        finally:
            store.close()

    def analytics(self, period: str) -> dict[str, Any]:
        store = AuditStore(self.db_path)
        try:
            return build_analytics(store, period)
        finally:
            store.close()

    def setup(self) -> dict[str, Any]:
        settings = load_settings(self.config_path)
        store = AuditStore(self.db_path)
        try:
            control = store.paper_control()
        finally:
            store.close()
        return {
            "configuration": _json_safe(asdict(settings)),
            "control": control,
            "allowed_actions": ["observe", "paper", "pause", "resume", "export"],
            "forbidden_actions": ["shell", "testnet_order", "production_order", "config_mutation_with_active_db"],
            "orders_enabled": False,
        }

    def export(self, kind: str) -> dict[str, Any]:
        store = AuditStore(self.db_path)
        try:
            if kind == "paper":
                return store.latest_paper_report() or {"status": "no PAPER report"}
            if kind == "observation":
                return store.latest_report() or {"status": "no observation report"}
            raise ValueError("export kind must be paper or observation")
        finally:
            store.close()


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "PaperDashboard/0.5"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(15)

    def version_string(self) -> str:
        return self.server_version

    @property
    def app(self) -> DashboardApplication:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        print(f"dashboard {self.client_address[0]} {format % args}")

    def _security_headers(self, nonce: str | None = None) -> None:
        self.send_header("Cache-Control", "no-store")
        if nonce:
            policy = (
                "default-src 'none'; "
                f"script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
                "base-uri 'none'; form-action 'self'"
            )
        else:
            policy = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        self.send_header("Content-Security-Policy", policy)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

    def _authenticate(self) -> bool:
        if self.app.authorized(self.headers.get("Authorization")):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="PAPER Dashboard", charset="UTF-8"')
        self._security_headers()
        self.end_headers()
        return False

    def _send_json(self, payload: Any, status: int = 200, *, attachment: str | None = None) -> None:
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        if attachment:
            self.send_header("Content-Disposition", f'attachment; filename="{attachment}"')
        self._security_headers()
        self.end_headers()
        self.wfile.write(raw)

    def _send_html(self) -> None:
        nonce = secrets.token_urlsafe(18)
        raw = (
            DASHBOARD_HTML.replace("__CSRF__", self.app.csrf_token)
            .replace("__NONCE__", nonce)
            .encode("utf-8")
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self._security_headers(nonce)
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise ValueError("invalid content length") from None
        if length <= 0 or length > MAX_BODY_BYTES:
            raise ValueError("invalid request body size")
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise ValueError("invalid JSON body") from None
        if not isinstance(payload, dict):
            raise ValueError("JSON body must be an object")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path)
        if path.path == "/health":
            self._send_json({"status": "ok", "mode": "PAPER", "orders_enabled": False})
            return
        if not self._authenticate():
            return
        try:
            if path.path in {"/", "/setup"}:
                self._send_html()
            elif path.path == "/api/status":
                self._send_json(self.app.status())
            elif path.path == "/api/analytics":
                period = parse_qs(path.query).get("period", ["month"])[0]
                self._send_json(self.app.analytics(period))
            elif path.path == "/api/setup":
                self._send_json(self.app.setup())
            elif path.path == "/api/export":
                kind = parse_qs(path.query).get("kind", ["paper"])[0]
                self._send_json(self.app.export(kind), attachment=f"{kind}-report.json")
            else:
                self._send_json({"error": "not found"}, 404)
        except sqlite3.Error:
            self._send_json({"error": "database temporarily unavailable"}, 503)
        except (ValueError, OSError) as exc:
            self._send_json({"error": str(exc)[:500]}, 400)

    def do_POST(self) -> None:  # noqa: N802
        if not self._authenticate():
            return
        if not hmac.compare_digest(self.headers.get("X-CSRF-Token", ""), self.app.csrf_token):
            self._send_json({"error": "invalid CSRF token"}, 403)
            return
        try:
            payload = self._body()
            path = urlsplit(self.path).path
            if path == "/api/actions/run":
                action = payload.get("action")
                if not isinstance(action, str) or action not in {"observe", "paper"}:
                    raise ValueError("invalid predefined action")
                if not self.app.start_cycle(action):
                    self._send_json({"error": "another action is already running"}, 409)
                    return
                self._send_json({"accepted": True, "action": action, "orders_enabled": False}, 202)
            elif path == "/api/actions/pause":
                reason = payload.get("reason")
                if reason is not None and not isinstance(reason, str):
                    raise ValueError("pause reason must be text")
                self._send_json(self.app.set_paused(True, reason))
            elif path == "/api/actions/resume":
                self._send_json(self.app.set_paused(False, "manually resumed from authenticated dashboard"))
            else:
                self._send_json({"error": "not found"}, 404)
        except sqlite3.Error:
            self._send_json({"error": "database temporarily unavailable"}, 503)
        except (ValueError, OSError) as exc:
            self._send_json({"error": str(exc)[:500]}, 400)

    def _method_not_allowed(self) -> None:
        self.send_response(HTTPStatus.METHOD_NOT_ALLOWED)
        self.send_header("Allow", "GET, POST")
        self._security_headers()
        self.end_headers()

    do_HEAD = _method_not_allowed
    do_OPTIONS = _method_not_allowed
    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed
    do_TRACE = _method_not_allowed


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], app: DashboardApplication) -> None:
        super().__init__(address, DashboardHandler)
        self.app = app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Authenticated PAPER-only dashboard")
    parser.add_argument("--config", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument("--auth-token-file", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if not 1 <= args.port <= 65535:
        raise SystemExit("invalid port")
    token = _read_token(Path(args.auth_token_file))
    app = DashboardApplication(Path(args.config), Path(args.db), token)
    server = DashboardServer((args.host, args.port), app)
    print(f"PAPER dashboard listening on {args.host}:{args.port}; orders_enabled=false")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


DASHBOARD_HTML = r'''<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="csrf-token" content="__CSRF__"><title>Cashmaxx PAPER Dashboard</title>
<style nonce="__NONCE__">
:root{--bg:#08111f;--panel:#101c2d;--line:#26364d;--text:#e8eef8;--muted:#91a3bc;--green:#3ddc97;--red:#ff667a;--amber:#ffc857;--blue:#66a3ff}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px system-ui,Arial}.wrap{max-width:1440px;margin:auto;padding:20px}.banner{background:#17253a;border:1px solid var(--line);padding:12px;border-radius:10px;margin-bottom:16px}.banner b{color:var(--green)}nav{display:flex;gap:8px;flex-wrap:wrap;margin:12px 0}button,.button{border:1px solid var(--line);background:#16263d;color:var(--text);padding:9px 13px;border-radius:8px;cursor:pointer;text-decoration:none}button:hover,.button:hover{border-color:var(--blue)}button.danger{border-color:#653343;color:#ffb2bd}.active{background:#234873}.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}.card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px;overflow:auto}.metric{font-size:25px;font-weight:700;margin-top:6px}.muted{color:var(--muted)}.green{color:var(--green)}.red{color:var(--red)}.amber{color:var(--amber)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:8px;border-bottom:1px solid var(--line);white-space:nowrap}section{display:none;margin-top:12px}section.show{display:block}.wide{grid-column:span 2}.full{grid-column:1/-1}canvas{width:100%;height:240px;background:#0b1626;border-radius:8px}pre{white-space:pre-wrap;word-break:break-word}.status{padding:8px;border-radius:7px;background:#0b1626;margin:8px 0}@media(max-width:900px){.grid{grid-template-columns:1fr 1fr}.wide{grid-column:span 2}}@media(max-width:560px){.grid{grid-template-columns:1fr}.wide,.full{grid-column:span 1}}
</style></head><body><div class="wrap">
<h1>Cashmaxx — PAPER Analytics</h1><div class="banner"><b>Dados:</b> Binance Spot público &nbsp; <b>Execução:</b> PAPER &nbsp; <b>Alvo futuro:</b> Binance Spot Testnet &nbsp; <b>Ordens:</b> NÃO HABILITADAS</div>
<nav><button data-tab="overview" class="active">Visão geral</button><button data-tab="performance">Resultados</button><button data-tab="trends">Tendências</button><button data-tab="setup">Setup e ações</button></nav>
<div id="periods"><button data-period="day">Dia</button><button data-period="week">Semana</button><button data-period="month" class="active">Mês</button><button data-period="all">Tudo</button></div>
<section id="overview" class="show"><div class="grid"><div class="card"><span class="muted">Patrimônio</span><div id="equity" class="metric">—</div></div><div class="card"><span class="muted">Resultado do período</span><div id="pnl" class="metric">—</div></div><div class="card"><span class="muted">Retorno</span><div id="return" class="metric">—</div></div><div class="card"><span class="muted">Drawdown máximo</span><div id="drawdown" class="metric">—</div></div><div class="card wide"><h3>Curva de patrimônio</h3><canvas id="chart" width="800" height="240"></canvas></div><div class="card wide"><h3>Posições abertas</h3><table><thead><tr><th>Ativo</th><th>Entrada</th><th>Marca</th><th>P&L</th><th>Stop</th></tr></thead><tbody id="positions"></tbody></table></div></div></section>
<section id="performance"><div class="grid"><div class="card"><span class="muted">Trades encerrados</span><div id="trades" class="metric">—</div></div><div class="card"><span class="muted">Taxa de acerto</span><div id="winrate" class="metric">—</div></div><div class="card"><span class="muted">Melhor ativo</span><div id="best" class="metric">—</div></div><div class="card"><span class="muted">Pior ativo</span><div id="worst" class="metric">—</div></div><div class="card full"><h3>Resultado por ativo</h3><table><thead><tr><th>Ativo</th><th>Trades</th><th>Vitórias</th><th>Perdas</th><th>P&L</th></tr></thead><tbody id="assets"></tbody></table></div><div class="card full"><h3>Resultados diários</h3><table><thead><tr><th>Dia UTC</th><th>Patrimônio</th><th>P&L diário</th><th>%</th></tr></thead><tbody id="daily"></tbody></table></div></div></section>
<section id="trends"><div class="grid"><div class="card wide"><h3>Novas tendências e sinais</h3><table><thead><tr><th>Ativo</th><th>Sinal</th><th>Razões</th></tr></thead><tbody id="emerging"></tbody></table></div><div class="card wide"><h3>Ranking elegível</h3><table><thead><tr><th>#</th><th>Ativo</th><th>Score</th><th>30 dias</th></tr></thead><tbody id="ranking"></tbody></table></div><div class="card full"><h3>Rejeições de dados</h3><pre id="rejections">—</pre></div></div></section>
<section id="setup"><div class="grid"><div class="card wide"><h3>Controles operacionais</h3><div id="job" class="status">Nenhuma ação em execução</div><button id="runObserve">Executar OBSERVATION</button> <button id="runPaper">Executar PAPER</button><br><br><input id="pauseReason" placeholder="Motivo da pausa" maxlength="200"> <button id="pauseEntries" class="danger">Pausar entradas</button> <button id="resumeEntries">Retomar entradas</button></div><div class="card wide"><h3>Exportação</h3><a class="button" href="/api/export?kind=paper">Relatório PAPER</a> <a class="button" href="/api/export?kind=observation">Observação</a><p class="muted">Não existe terminal, comando arbitrário ou botão de ordem.</p></div><div class="card full"><h3>Configuração validada (somente leitura)</h3><pre id="config">Carregando…</pre></div></div></section>
</div><script nonce="__NONCE__">
const csrf=document.querySelector('meta[name=csrf-token]').content;let period='month',data=null;const q=s=>document.querySelector(s);function txt(id,v){q(id).textContent=v??'—'}function money(v){return `${Number(v||0).toFixed(4)} USDT`}function color(el,v){el.classList.remove('green','red');el.classList.add(Number(v)>=0?'green':'red')}function rows(id,items,fn){const body=q(id);body.textContent='';for(const item of items){const tr=document.createElement('tr');for(const value of fn(item)){const td=document.createElement('td');td.textContent=value??'';tr.appendChild(td)}body.appendChild(tr)}}
async function api(url,opt={}){const r=await fetch(url,opt);const j=await r.json();if(!r.ok)throw new Error(j.error||`HTTP ${r.status}`);return j}function draw(series){const c=q('#chart'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);if(!series.length)return;const vals=series.map(v=>Number(v.equity_usdt));const lo=Math.min(...vals),hi=Math.max(...vals),span=hi-lo||1;x.strokeStyle='#3ddc97';x.lineWidth=3;x.beginPath();vals.forEach((v,i)=>{const px=20+i*(c.width-40)/Math.max(1,vals.length-1),py=20+(hi-v)*(c.height-40)/span;i?x.lineTo(px,py):x.moveTo(px,py)});x.stroke()}
async function load(){data=await api(`/api/analytics?period=${period}`);txt('#equity',money(data.final_equity_usdt));txt('#pnl',money(data.period_pnl_usdt));color(q('#pnl'),data.period_pnl_usdt);txt('#return',`${Number(data.period_return_pct).toFixed(3)}%`);color(q('#return'),data.period_return_pct);txt('#drawdown',`${Number(data.max_drawdown_pct).toFixed(3)}%`);txt('#trades',data.closed_trades);txt('#winrate',`${Number(data.win_rate_pct).toFixed(2)}%`);txt('#best',data.best_asset?`${data.best_asset.symbol} (${money(data.best_asset.realized_pnl_usdt)})`:'—');txt('#worst',data.worst_asset?`${data.worst_asset.symbol} (${money(data.worst_asset.realized_pnl_usdt)})`:'—');rows('#positions',data.open_positions,p=>[p.symbol,p.entry_price,p.mark_price,p.unrealized_pnl_usdt,p.trailing_stop||p.hard_stop]);rows('#assets',data.asset_performance,a=>[a.symbol,a.trades,a.wins,a.losses,a.realized_pnl_usdt]);rows('#daily',data.equity_series,d=>[d.risk_day_utc,d.equity_usdt,d.daily_pnl_usdt,d.daily_pnl_pct]);rows('#emerging',data.emerging_trends,t=>[t.symbol,t.signal,(t.reasons||[]).join('; ')]);rows('#ranking',data.eligible_ranked,r=>[r.rank,r.symbol,r.score,r.return_30d_pct]);txt('#rejections',JSON.stringify(data.skipped_reasons,null,2));draw(data.equity_series)}
async function setup(){const s=await api('/api/setup');txt('#config',JSON.stringify(s,null,2));await poll()}async function post(url,body){return api(url,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:JSON.stringify(body)})}async function runAction(action){try{await post('/api/actions/run',{action});await poll()}catch(e){alert(e.message)}}async function pauseEntries(){try{await post('/api/actions/pause',{reason:q('#pauseReason').value});await setup()}catch(e){alert(e.message)}}async function resumeEntries(){try{await post('/api/actions/resume',{});await setup()}catch(e){alert(e.message)}}async function poll(){const s=await api('/api/status');txt('#job',s.job.running?`Executando ${s.job.action}…`:s.job.error?`Falha: ${s.job.error}`:s.job.result?`Concluído: ${JSON.stringify(s.job.result)}`:`Entradas pausadas: ${s.control.paused}`);if(s.job.running)setTimeout(poll,3000);else load()}
document.querySelectorAll('[data-tab]').forEach(b=>b.addEventListener('click',()=>{document.querySelectorAll('section').forEach(s=>s.classList.remove('show'));q('#'+b.dataset.tab).classList.add('show');document.querySelectorAll('[data-tab]').forEach(x=>x.classList.remove('active'));b.classList.add('active');if(b.dataset.tab==='setup')setup()}));document.querySelectorAll('[data-period]').forEach(b=>b.addEventListener('click',()=>{period=b.dataset.period;document.querySelectorAll('[data-period]').forEach(x=>x.classList.remove('active'));b.classList.add('active');load()}));q('#runObserve').addEventListener('click',()=>runAction('observe'));q('#runPaper').addEventListener('click',()=>runAction('paper'));q('#pauseEntries').addEventListener('click',pauseEntries);q('#resumeEntries').addEventListener('click',resumeEntries);load();poll();
</script></body></html>'''


if __name__ == "__main__":
    raise SystemExit(main())
