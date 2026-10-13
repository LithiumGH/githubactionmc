#!/usr/bin/env python3
"""Launcher Web UI backend — stdlib only (no Flask needed)."""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.parse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import mc_core

ROOT = Path(__file__).parent
PUBLIC = ROOT / "public"
STATE = {}
TUNNELS = {"launcher": os.environ.get("LAUNCHER_URL", ""), "vnc": os.environ.get("VNC_URL", "")}

def progress_logger(tag):
    d = mc_core.mc_dir()
    (d / "logs").mkdir(parents=True, exist_ok=True)
    lp = d / "logs" / f"{tag}.log"

    def _log(m):
        STATE["log_" + tag] = str(m)[:300]
        with open(lp, "a") as f:
            f.write(str(m) + "\n")
    return _log

def sh_json(handler, obj, code=200):
    body = json.dumps(obj).encode()
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        sys.stderr.write("%s %s\n" % (self.command, self.path))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            p = PUBLIC / "index.html"
            body = p.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/status":
            d = mc_core.mc_dir()
            pid = None
            if (d / "game.pid").exists():
                try:
                    pid = int((d / "game.pid").read_text().strip())
                    os.kill(pid, 0)
                except Exception:
                    pid = None
            last = {}
            if (d / "last_install.json").exists():
                last = json.loads((d / "last_install.json").read_text())
            sh_json(self, {"running": pid, "last": last, "mc_dir": str(d),
                           "fakegpu": Path("/etc/fakegpu/env").exists(),
                           "tunnels": TUNNELS, "state": STATE})
        elif u.path == "/api/versions":
            want = q.get("q", [""])[0]
            m = mc_core.http_get_json(mc_core.MOJANG_MANIFEST)
            vers = [v["id"] for v in m["versions"] if want in v["id"]]
            sh_json(self, {"latest": m["latest"], "versions": vers[:50]})
        elif u.path == "/api/log":
            d = mc_core.mc_dir()
            which = q.get("file", ["game"])[0]
            name = {"game": "game.log", "install": "install.log", "mrpack": "mrpack.log"}.get(which, "game.log")
            log = d / "logs" / name
            n = int(q.get("lines", ["200"])[0])
            if log.exists():
                lines = log.read_text(errors="replace").splitlines()[-n:]
            else:
                lines = [f"(no {name} yet — run install/launch first)"]
            sh_json(self, {"lines": lines})
        else:
            self.send_error(404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        ln = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(ln) or b"{}")
        except Exception:
            body = {}
        if u.path == "/api/install":
            def run():
                try:
                    STATE["install"] = "running"
                    r = mc_core.install_loader(body.get("mc_version", "1.21"),
                                               body.get("loader", "fabric"),
                                               body.get("loader_version", "latest"),
                                               progress=progress_logger("install"))
                    STATE["install"] = "done"
                    STATE["install_result"] = {k: str(v) for k, v in r.items() if k in ("version", "loader", "loader_version")}
                except Exception as e:  # noqa
                    STATE["install"] = f"error: {e}"
                    try:
                        progress_logger("install")(f"ERROR: {e}")
                    except Exception:
                        pass
            threading.Thread(target=run, daemon=True).start()
            sh_json(self, {"started": True})
        elif u.path == "/api/launch":
            try:
                r = mc_core.launch(body.get("mc_version", "1.21"), body.get("loader", "fabric"),
                                   body.get("username", "Player"), body.get("ram", "4096M"),
                                   body.get("resolution", "1280x720"))
                sh_json(self, r)
            except Exception as e:
                sh_json(self, {"error": str(e)}, 500)
        elif u.path == "/api/stop":
            d = mc_core.mc_dir()
            try:
                pid = int((d / "game.pid").read_text().strip())
                os.kill(pid, signal.SIGTERM)
                sh_json(self, {"stopped": pid})
            except Exception as e:
                sh_json(self, {"error": str(e)}, 500)
        elif u.path == "/api/mod":
            act = body.get("action")
            try:
                if act == "search":
                    hits = mc_core.modrinth_search(body.get("query", ""),
                                                   [body.get("loader", "fabric")] if body.get("loader") != "vanilla" else None,
                                                   body.get("mc_version"), limit=12)
                    sh_json(self, {"hits": [{"slug": h["slug"], "title": h["title"], "downloads": h["downloads"], "desc": h["description"][:120]} for h in hits]})
                elif act == "install":
                    p = mc_core.modrinth_install(body["slug"], body["mc_version"], body.get("loader", "fabric"))
                    sh_json(self, {"installed": str(p)})
                elif act == "perf":
                    mc, ld = body["mc_version"], body.get("loader", "fabric")
                    out = []
                    for slug in mc_core.PERF_MODS.get(ld, []):
                        try:
                            out.append(str(mc_core.modrinth_install(slug, mc, ld)))
                        except Exception as e:
                            out.append(f"{slug}: {e}")
                    sh_json(self, {"installed": out})
                else:
                    sh_json(self, {"error": "unknown action"}, 400)
            except Exception as e:
                sh_json(self, {"error": str(e)}, 500)
        elif u.path == "/api/mrpack":
            def run():
                try:
                    STATE["mrpack"] = "running"
                    r = mc_core.install_mrpack(body["url"], mc_version=body.get("mc_version"), loader=body.get("loader"),
                                               progress=progress_logger("mrpack"))
                    STATE["mrpack"] = f"done: {r}"
                except Exception as e:
                    STATE["mrpack"] = f"error: {e}"
                    try:
                        progress_logger("mrpack")(f"ERROR: {e}")
                    except Exception:
                        pass
            threading.Thread(target=run, daemon=True).start()
            sh_json(self, {"started": True})
        else:
            self.send_error(404)

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--dir", default=None)
    a = ap.parse_args()
    if a.dir:
        os.environ["MC_DIR"] = a.dir
    print(f"[launcher] serving {PUBLIC}/index.html on :{a.port}, mc_dir={mc_core.mc_dir()}", flush=True)
    ThreadingHTTPServer(("0.0.0.0", a.port), H).serve_forever()
