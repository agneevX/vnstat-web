#!/usr/bin/env python3
"""vnStat dashboard server. Stdlib only (Python 3.11+ for TOML, or `pip install tomli`).

  python3 server.py                     # config.toml next to this file, port 8080
  python3 server.py --config my.toml --port 9000
  python3 server.py --demo              # first run only: creates a config with fake demo servers

A server in the config can be:
  /var/lib/vnstat/vnstat.db             path to a vnStat SQLite database, opened read-only (also sqlite:/path)
  http(s)://host:8685/json.cgi          vergoh/vnstat docker image
  local:                                run `vnstat --json` on this machine
  demo:<anything>                       generated sample data
"""
import argparse, calendar, json, math, os, random, re, sqlite3, subprocess, threading, time, urllib.error, urllib.request, zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib
    except ModuleNotFoundError:
        tomllib = None

ROOT = Path(__file__).parent
ERR_TTL = 3.0  # seconds a failed upstream result is reused (stops hammering a dead server)


# ───────────────────────────── demo data ─────────────────────────────
def demo_data(seed_str):
    now = datetime.now().replace(second=0, microsecond=0)
    rnd = random.Random(zlib.crc32(seed_str.encode()))
    k = 0.4 + rnd.random() * 1.2

    def diurnal(h):  # busiest around 9pm
        return 0.3 + 0.7 * (1 + math.cos((h - 21) / 24 * 2 * math.pi)) / 2

    def entry(dt, scale, hourly):
        f = diurnal(dt.hour) if hourly else (1.4 if dt.weekday() >= 5 else 1.0)
        rx = int(rnd.random() * scale * f * 1.2)
        e = {"id": 0, "date": {"year": dt.year, "month": dt.month, "day": dt.day},
             "rx": rx, "tx": int(rx * (0.08 + rnd.random() * 0.12))}
        if hourly:
            e["time"] = {"hour": dt.hour, "minute": dt.minute}
        return e

    def series(base, n, step, scale, hourly, gap=None):
        # gap=(a, b): skip entries a..b steps ago, like a host that was switched off
        return [entry(base - step * i, scale, hourly) for i in range(n - 1, -1, -1) if not (gap and gap[0] <= i <= gap[1])]

    def months(n, scale):
        out, y, m = [], now.year, now.month
        for _ in range(n):
            rx = int(rnd.random() * scale)
            out.append({"id": 0, "date": {"year": y, "month": m}, "rx": rx, "tx": int(rx * 0.12)})
            m -= 1
            if m == 0:
                m, y = 12, y - 1
        return out[::-1]

    def years():
        out = []
        for y in (now.year - 1, now.year):
            rx = int(rnd.random() * 6e12 * k) + int(2e12 * k)
            out.append({"id": 0, "date": {"year": y}, "rx": rx, "tx": int(rx * 0.13)})
        return out

    def iface(name, f):
        t = {
            # vnStat aligns buckets: 5-min on :00/:05/..., hours on :00, days at midnight
            "fiveminute": series(now.replace(minute=now.minute - now.minute % 5), 576, timedelta(minutes=5), 60e6 * f * k, True, (204, 420)),
            "hour": series(now.replace(minute=0), 24 * 14, timedelta(hours=1), 700e6 * f * k, True, (20, 62)),
            "day": series(now.replace(hour=0, minute=0), 400, timedelta(days=1), 14e9 * f * k, False, (9, 12)),
            "month": months(24, 420e9 * f * k),
            "year": years(),
        }
        t["total"] = {"rx": sum(e["rx"] for e in t["year"]), "tx": sum(e["tx"] for e in t["year"])}
        first = now - timedelta(days=399)
        return {"name": name, "alias": "", "traffic": t,
                "created": {"date": {"year": first.year, "month": first.month, "day": first.day}}}

    return {"vnstatversion": "demo", "jsonversion": "2",
            "interfaces": [iface("eth0", 1), iface("wlan0", 0.3), iface("tailscale0", 0.08)]}


# ───────────────────────────── vnStat SQLite database ─────────────────────────────
class SourceError(Exception):
    """A problem with a data source that already has a user-readable message."""


def _when(v, utc):
    """vnStat stores a DATE as 'YYYY-MM-DD[ HH:MM:SS]' text (or an epoch). Returns a naive local datetime.
    5-minute and hourly rows are UTC and converted to this machine's timezone; day/month/year rows are
    already calendar dates."""
    if isinstance(v, (int, float)) or re.fullmatch(r"\d{9,}", str(v).strip()):
        return datetime.fromtimestamp(int(v))
    t = str(v).strip().replace("T", " ")
    if len(t) >= 19:
        dt = datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
        return dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None) if utc else dt
    return datetime.strptime(t[:10], "%Y-%m-%d")


def _epoch(v):
    """UTC epoch seconds of a vnStat 5-minute/hourly DATE value."""
    if isinstance(v, (int, float)) or re.fullmatch(r"\d{9,}", str(v).strip()):
        return int(v)
    return calendar.timegm(datetime.strptime(str(v).strip().replace("T", " ")[:19], "%Y-%m-%d %H:%M:%S").timetuple())


def read_sqlite(path):
    """Builds the same structure as `vnstat --json` straight from a vnStat 2.x database file."""
    p = Path(path).expanduser()
    if not p.is_file():
        raise SourceError(f"database not found: {p}")
    base = p.resolve().as_uri()
    try:
        con = sqlite3.connect(base + "?mode=ro", uri=True, timeout=5)
        con.execute("select 1 from interface limit 1")
    except sqlite3.OperationalError:
        # read-only directories can't hold WAL's -shm file; fall back to a plain snapshot read
        con = sqlite3.connect(base + "?immutable=1", uri=True, timeout=5)
    try:
        con.execute("pragma query_only = on")   # belt and braces: this process never writes to the database
        try:
            info = dict(con.execute("select name, value from info").fetchall())
        except sqlite3.Error:
            info = {}
        try:
            rows = con.execute("select id, name, alias, created, rxtotal, txtotal from interface order by id").fetchall()
        except sqlite3.Error as e:
            raise SourceError(f"{p} doesn't look like a vnStat database ({e})")
        out = []
        for iid, name, alias, created, rxt, txt in rows:
            traffic = {"total": {"rx": rxt or 0, "tx": txt or 0}}
            for tbl, timed in (("fiveminute", True), ("hour", True), ("day", False), ("month", False), ("year", False)):
                try:
                    data = con.execute(f"select date, rx, tx from {tbl} where interface=? order by date", (iid,)).fetchall()
                except sqlite3.Error:
                    data = []
                lst = []
                for d, rx, tx in data:
                    dt = _when(d, timed)
                    e = {"id": len(lst), "rx": rx or 0, "tx": tx or 0}
                    e["date"] = ({"year": dt.year, "month": dt.month, "day": dt.day} if tbl in ("fiveminute", "hour", "day")
                                 else {"year": dt.year, "month": dt.month} if tbl == "month" else {"year": dt.year})
                    if timed:
                        e["time"] = {"hour": dt.hour, "minute": dt.minute}
                        try:  # exact instant, so clients can show it in their own timezone
                            e["timestamp"] = _epoch(d)
                        except ValueError:
                            pass
                    lst.append(e)
                traffic[tbl] = lst
            item = {"name": name, "alias": alias or "", "traffic": traffic}
            try:
                c = _when(created, False)
                item["created"] = {"date": {"year": c.year, "month": c.month, "day": c.day}}
            except (ValueError, TypeError):
                pass
            out.append(item)
        return {"vnstatversion": str(info.get("vnstatversion") or "sqlite"), "jsonversion": "2", "interfaces": out}
    finally:
        con.close()


def source_kind(url):
    if re.match(r"^https?://\S+$", url):
        return "http"
    if url.startswith("local:"):
        return "local"
    if url.startswith("demo:"):
        return "demo"
    if url.startswith("sqlite:") and len(url) > 7:
        return "sqlite"
    if re.match(r"^(/|\./|\.\./|~/|[A-Za-z]:[\\/])\S", url):
        return "sqlite"
    return None


# ───────────────────────────── config ─────────────────────────────
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:32] or "server"


def _str(v, field, maxlen=200):
    if not isinstance(v, str):
        raise ValueError(f"{field} must be text")
    v = _CTRL.sub("", v).strip()
    if len(v) > maxlen:
        raise ValueError(f"{field} is too long")
    return v


def validate(cfg):
    if not isinstance(cfg, dict) or not isinstance(cfg.get("servers"), list):
        raise ValueError("expected {servers: [...]}")
    if not 1 <= len(cfg["servers"]) <= 20:
        raise ValueError("need between 1 and 20 servers")
    out, seen = [], set()
    for i, s in enumerate(cfg["servers"], 1):
        if not isinstance(s, dict):
            raise ValueError(f"server {i} is invalid")
        name = _str(s.get("name", ""), f"server {i} name", 64)
        url = _str(s.get("url", ""), f"server {i} URL", 500)
        if not name:
            raise ValueError(f"server {i} needs a name")
        if not source_kind(url):
            raise ValueError(f"server {i}: enter an http(s):// URL or the path of a vnStat database, e.g. /data/vnstat.db")
        sid = s.get("id")
        sid = sid if isinstance(sid, str) and _ID.match(sid) else _slug(name)
        base, n = sid, 2
        while sid in seen:
            sid, n = f"{base}-{n}", n + 1
        seen.add(sid)
        ifaces = {}
        raw = s.get("interfaces") or {}
        if not isinstance(raw, dict):
            raise ValueError(f"server {i} interfaces must be a map")
        for key, val in raw.items():
            key, val = _str(key, "interface name", 64), _str(val, "friendly name", 64)
            if key and val:
                ifaces[key] = val
        out.append({"id": sid, "name": name, "url": url, "interfaces": ifaces})
    return {"servers": out}


def _q(s):
    return json.dumps(s, ensure_ascii=False)


def dump_toml(cfg):
    lines = ["# vnStat dashboard config. Edited from the Settings dialog (comments are not preserved).", ""]
    for s in cfg["servers"]:
        lines += ["[[servers]]", f"id = {_q(s['id'])}", f"name = {_q(s['name'])}", f"url = {_q(s['url'])}"]
        if s["interfaces"]:
            lines += ["", "[servers.interfaces]"] + [f"{_q(k)} = {_q(v)}" for k, v in s["interfaces"].items()]
        lines.append("")
    return "\n".join(lines)


# ───────────────────────────── app ─────────────────────────────
class App:
    def __init__(self, args):
        self.args = args
        self.path = Path(args.config)
        self.lock = threading.Lock()
        self.cache = {}
        self.cfg = self._load()

    def _default(self):
        if self.args.demo:
            servers = [{"id": "demo-home", "name": "Home (demo)", "url": "demo:home", "interfaces": {"eth0": "Ethernet"}},
                       {"id": "demo-office", "name": "Office (demo)", "url": "demo:office", "interfaces": {}}]
        else:
            servers = [{"id": "local", "name": "vnStat",
                        "url": "local:" if self.args.local else self.args.url, "interfaces": {}}]
        return {"servers": servers}

    def _load(self):
        if tomllib is None:
            raise SystemExit("Need Python 3.11+ (or `pip install tomli`) to read the TOML config.")
        if not self.path.exists():
            self._write(self._default())
            print(f"Created {self.path}")
        with open(self.path, "rb") as f:
            return validate(tomllib.load(f))

    def _write(self, cfg):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(dump_toml(cfg), encoding="utf-8")
        os.replace(tmp, self.path)

    def save(self, new):
        cfg = validate(new)
        with self.lock:
            self._write(cfg)
            self.cfg = cfg
            self.cache.clear()
        return cfg

    def server(self, sid):
        servers = self.cfg["servers"]
        if not sid:
            return servers[0]
        return next((s for s in servers if s["id"] == sid), None)

    # -- upstream --
    def _fetch(self, srv, timeout):
        url = srv["url"]
        kind = source_kind(url)
        if kind == "demo":
            return demo_data(url)
        if kind == "sqlite":
            return read_sqlite(url[7:] if url.startswith("sqlite:") else url)
        if kind == "local":
            out = subprocess.run([self.args.vnstat, "--json"], capture_output=True, text=True,
                                 timeout=timeout, check=True).stdout
            data = json.loads(out)
        else:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        if not isinstance(data, dict) or "interfaces" not in data:
            raise ValueError("response is not vnStat JSON")
        return data

    def data(self, srv, boot=False):
        """Returns (bytes, status). Cached per server; concurrent callers share one upstream request."""
        with self.lock:
            ent = self.cache.setdefault(srv["id"], {"url": None, "t": 0.0, "body": None, "code": 200, "lock": threading.Lock()})
        with ent["lock"]:
            ttl = self.args.cache if ent["code"] == 200 else ERR_TTL
            if ent["body"] and ent["url"] == srv["url"] and time.time() - ent["t"] < ttl:
                return ent["body"], ent["code"]
            try:
                body, code = json.dumps(self._fetch(srv, 2 if boot else 15)).encode(), 200
            except SourceError as e:
                body, code = json.dumps({"error": str(e)}).encode(), 502
            except FileNotFoundError:
                body, code = json.dumps({"error": f"vnstat binary not found: {self.args.vnstat}"}).encode(), 502
            except urllib.error.URLError as e:
                body, code = json.dumps({"error": f"cannot reach {srv['url']}: {getattr(e, 'reason', e)}"}).encode(), 502
            except subprocess.CalledProcessError as e:
                body, code = json.dumps({"error": f"vnstat failed: {(e.stderr or '').strip() or e.returncode}"}).encode(), 502
            except sqlite3.Error as e:
                body, code = json.dumps({"error": f"cannot read database: {e}"}).encode(), 502
            except PermissionError as e:
                body, code = json.dumps({"error": f"permission denied: {e.filename or e}"}).encode(), 502
            except Exception as e:  # noqa: BLE001 - report anything to the client
                body, code = json.dumps({"error": f"{type(e).__name__}: {e}"}).encode(), 502
            if code == 200 or not boot:  # a quick page-load probe must not poison the cache with its short timeout
                ent.update(url=srv["url"], t=time.time(), body=body, code=code)
            return body, code


def make_handler(app):
    class H(BaseHTTPRequestHandler):
        def _send(self, body, code=200, ctype="application/json"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(json.dumps(obj).encode(), code)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                html = (ROOT / "index.html").read_text(encoding="utf-8")
                esc = lambda s: s.replace("<", "\\u003c")  # keeps JSON from closing the script tag
                srv = app.server("")
                body, code = app.data(srv, boot=True)
                js = "window.__CONFIG__=" + esc(json.dumps(app.cfg)) + ";"
                if code == 200:
                    js += "window.__BOOT__={server:" + esc(json.dumps(srv["id"])) + ",data:" + esc(body.decode()) + "};"
                self._send(html.replace("<!--BOOT-->", "<script>" + js + "</script>").encode(), 200, "text/html; charset=utf-8")
            elif u.path == "/api/config":
                self._json(app.cfg)
            elif u.path == "/api/vnstat":
                sid = (parse_qs(u.query).get("server") or [""])[0]
                srv = app.server(sid)
                if not srv:
                    return self._json({"error": "unknown server"}, 404)
                body, code = app.data(srv)
                self._send(body, code)
            else:
                self._json({"error": "not found"}, 404)

        def do_PUT(self):
            if urlparse(self.path).path != "/api/config":
                return self._json({"error": "not found"}, 404)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                if n <= 0 or n > 65536:
                    raise ValueError("bad body size")
                self._json(app.save(json.loads(self.rfile.read(n))))
            except (ValueError, json.JSONDecodeError) as e:
                self._json({"error": str(e)}, 400)
            except OSError as e:
                self._json({"error": f"cannot write config: {e}"}, 500)

        def log_message(self, *a):
            pass

    return H


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--config", default=str(ROOT / "config.toml"), help="TOML file holding servers and friendly names")
    p.add_argument("--url", default="/var/lib/vnstat/vnstat.db", help="first server (a vnStat database path or json.cgi URL) used when creating a new config")
    p.add_argument("--local", action="store_true", help="when creating a new config, use the local vnstat binary")
    p.add_argument("--vnstat", default="vnstat", help="vnstat binary for local: servers")
    p.add_argument("--cache", type=float, default=5, help="seconds to cache each server's response")
    p.add_argument("--demo", action="store_true", help="when creating a new config, use demo servers")
    a = p.parse_args()
    app = App(a)
    srv = ThreadingHTTPServer((a.host, a.port), make_handler(app))
    srv.daemon_threads = True
    print(f"Serving on http://{a.host}:{a.port}  (config: {a.config})")
    srv.serve_forever()