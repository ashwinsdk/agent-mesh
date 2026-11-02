"""Optional read-only web viewer over the same snapshot layer as the TUI. stdlib only, bound to 127.0.0.1.

Security: no write endpoints (non-GET => 405); all dynamic text is inserted with textContent (agent/message content is
untrusted, so it is never interpreted as HTML); Host header must be localhost (DNS-rebinding guard).
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import snapshot
from .db import Paths

PAGE = """<!doctype html><meta charset=utf-8><title>agent-mesh</title>
<style>body{font:13px ui-monospace,monospace;background:#0e1116;color:#d6dde6;margin:0}header{padding:8px 14px;background:#161b22;position:sticky;top:0}
section{margin:10px 14px}h2{font-size:13px;color:#58a6ff;margin:14px 0 4px}table{border-collapse:collapse;width:100%}td,th{padding:2px 8px;text-align:left;border-bottom:1px solid #222}
th{color:#8b949e}.bad{color:#ff7b72}.warn{color:#d29922}.ok{color:#3fb950}.dim{color:#8b949e}.bar{display:inline-block;width:120px;background:#222;height:8px}.bar i{display:block;height:8px;background:#3fb950}</style>
<header id=hd>agent-mesh (read-only)</header><main id=m></main>
<script>
const $=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!==undefined)e.textContent=String(x);return e};
const n=v=>v==null?'n/a':v>=1e6?(v/1e6).toFixed(2)+'M':v>=1e3?(v/1e3).toFixed(1)+'k':String(v);
const dur=s=>s==null?'-':s<90?s+'s':s<5400?Math.floor(s/60)+'m':(s/3600).toFixed(1)+'h';
const SC={WORKING:'ok',COMMUNICATING:'ok',REVIEWING:'ok',PLANNING:'ok',INITIALISING:'ok',FAILED:'bad',BLOCKED:'warn',PAUSED:'warn',NEEDS_REWORK:'bad',COMPLETED:'dim',TERMINATED:'dim',IDLE:'dim'};
function table(cols,rows,cls){const t=$('table');const h=$('tr');cols.forEach(c=>h.append($('th','',c)));t.append(h);
 rows.forEach(r=>{const tr=$('tr',cls?cls(r):'');cols.forEach((c,i)=>tr.append($('td','',r[i]==null?'-':r[i])));t.append(tr)});return t}
function render(s){const m=$('main');m.id='m';const p=s.project,sec=(t)=>{const e=$('section');e.append($('h2','',t));m.append(e);return e};
 document.getElementById('hd').textContent=`${p.name} — phase ${p.phase} — ${(p.progress*100).toFixed(0)}% (${p.completed_leaf}/${p.leaf_tasks}) — ${new Date().toLocaleTimeString()}`;
 let e=sec('ALERTS');(s.alerts.length?s.alerts:[{level:'dim',text:'none'}]).forEach(a=>e.append($('div',a.level,a.text)));
 e=sec('AGENTS');e.append(table(['id','name','cli/model','task','state','pid','dur','last','restarts','blocker/latest'],s.agents.map(a=>[a.id,a.name,a.cli+'/'+(a.model||'-'),a.task,a.state,a.pid,dur(a.duration),dur(a.last_activity_age),a.restarts,a.blocker||a.latest]),r=>SC[r[4]]||''));
 e=sec('TASKS');e.append(table(['id','title','status','owner','deps','risk','verified'],s.tasks.list.filter(t=>t.kind!='project').map(t=>[t.id,t.title,t.status,t.owner,t.deps.join(','),t.risk,t.verified==null?'-':t.verified?'yes':'FAILED']),r=>SC[r[2]]||''));
 e=sec('COMMUNICATION (pending acks: '+s.comms.pending_acks+', failures: '+s.comms.failures.length+')');e.append(table(['from','to','type','task','state','tries','text'],s.comms.recent.map(x=>[x.from,x.to,x.type,x.task,x.state,x.attempts,x.text]),r=>r[4]=='failed'?'bad':''));
 e=sec('USAGE (n/a = not exposed by the CLI)');const u=s.usage;const rows=[];Object.entries(u.per_cli).forEach(([k,a])=>rows.push(['cli:'+k,n(a.input),n(a.output),n(a.cache_read),a.tokens_known_fraction,'$'+a.api_spend.toFixed(4),'$'+a.informational_api_equiv.toFixed(4)]));
 Object.entries(u.per_model).forEach(([k,a])=>rows.push(['model:'+k,n(a.input),n(a.output),n(a.cache_read),a.tokens_known_fraction,'$'+a.api_spend.toFixed(4),'$'+a.informational_api_equiv.toFixed(4)]));
 Object.entries(u.per_agent).forEach(([k,a])=>rows.push(['agent:'+k,n(a.input),n(a.output),n(a.cache_read),a.tokens_known_fraction,'$'+a.api_spend.toFixed(4),'$'+a.informational_api_equiv.toFixed(4)]));
 e.append(table(['scope','input','output','cache read','tokens from','API spend','info api-equiv'],rows));
 u.notes.forEach(x=>e.append($('div','dim',x)));
 document.getElementById('m').replaceWith(m)}
const es=new EventSource('/stream');es.onmessage=ev=>render(JSON.parse(ev.data));es.onerror=()=>document.getElementById('hd').textContent='disconnected — retrying';
</script>"""


def make_server(paths: Paths, port: int = 8765) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _ok_host(self) -> bool:
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost", "[::1]")

        def _deny(self, code):
            self.send_response(code); self.end_headers()

        def do_GET(self):
            if not self._ok_host():
                return self._deny(403)
            if self.path == "/":
                b = PAGE.encode()
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
                self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            elif self.path == "/api/snapshot":
                b = json.dumps(snapshot.build(paths), default=str).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b)
            elif self.path == "/stream":
                self.send_response(200); self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-cache"); self.end_headers()
                c = sqlite3.connect(f"file:{paths.db}?mode=ro", uri=True, timeout=2)
                last_dv, last_send = None, 0.0
                try:
                    while True:
                        dv = c.execute("PRAGMA data_version").fetchone()[0]
                        if dv != last_dv or time.time() - last_send > 5:
                            self.wfile.write(b"data: " + json.dumps(snapshot.build(paths), default=str).encode() + b"\n\n"); self.wfile.flush()
                            last_dv, last_send = dv, time.time()
                        time.sleep(0.5)
                except (BrokenPipeError, ConnectionResetError, sqlite3.Error):
                    pass
                finally:
                    c.close()
            else:
                self._deny(404)

        def _method_not_allowed(self):
            self._deny(405)

        do_POST = do_PUT = do_DELETE = do_PATCH = _method_not_allowed

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


def serve(paths: Paths, port: int = 8765) -> None:
    srv = make_server(paths, port)
    print(f"agent-mesh dashboard (read-only) on http://127.0.0.1:{srv.server_address[1]}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
