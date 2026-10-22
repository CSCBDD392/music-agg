"""三家音乐源聚合网站 —— 零依赖（Python 标准库）的单页 Web 应用。

和 danmuji 仓库的关系：那边是桌面端点歌机 + 弹幕面板；这里是**网站**。
两边都靠同一批本地音乐 API（酷狗 4000 / 网易云 3000 / QQ 3300）工作，
所以这里的 source.py 是从 danmuji 抄过来的同一份适配逻辑（不想让网站依赖
桌面项目，宁可几十行重复）。想改适配规则两边一起改。

跑：

    python server.py                 # 打开 http://127.0.0.1:8800
    python server.py --port 9000
"""
import argparse
import json
import logging
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger("agg")
HERE = os.path.dirname(os.path.abspath(__file__))

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def _http(url, data=None, headers=None, timeout=10):
    h = {"User-Agent": UA, "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    body = None
    if data is not None and not isinstance(data, bytes):
        body = json.dumps(data).encode()
        h.setdefault("Content-Type", "application/json")
    else:
        body = data
    req = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return {}


def _strip(s):
    """酷狗会给搜索结果套 <em>/<0> 高亮标签，剥掉。"""
    if not s:
        return ""
    import re
    return re.sub(r"<[^>]+>", "", str(s)).strip()


def _score(song, kw):
    """同一套打分：标题/歌手命中加分，翻唱/伴奏/片段减分。"""
    t = (song.get("title") or "").lower()
    a = (song.get("artist") or "").lower()
    k = (kw or "").lower().strip()
    s = 0.0
    if k:
        if t == k:
            s += 20
        elif k in t:
            s += 12
        elif t and t in k:
            s += 6
        else:
            s -= 8
        if a and (a == k or k in a):
            s += 6
        elif a and a in k:
            s += 4
    for bad in ("live", "伴奏", "dj", "翻唱", "remix", "片段", "cover", "纯音乐"):
        if bad in t:
            s -= 5
    for bad in ("儿歌", "童声", "翻唱", "cover"):
        if bad in a:
            s -= 6
    if song.get("source") == "kugou":
        s += 1.0
    elif song.get("source") == "netease":
        s += 0.6
    elif song.get("source") == "qq":
        s += 0.2
    d = song.get("duration") or 0
    if 0 < d < 60:
        s -= 6
    return s


# ---------------------------------------------------------------- 三家适配
class Source(object):
    name = ""
    label = ""

    def __init__(self, host, port):
        self.base = "http://%s:%s" % (host or "127.0.0.1", port)
        self._up = None
        self.cookie = ""      # 登录后由 login.MusicLogin 注入

    def _headers(self):
        return {"Cookie": self.cookie} if self.cookie else {}

    def alive(self):
        if self._up is not None:
            return self._up
        try:
            urllib.request.urlopen(self.base + "/", timeout=3).read(64)
            self._up = True
        except urllib.error.HTTPError:
            self._up = True          # 有响应就算活着，404 也行
        except Exception:
            self._up = False
        return self._up

    def search(self, kw, n=6):
        raise NotImplementedError

    def url(self, song):
        raise NotImplementedError


class KugouSource(Source):
    name, label = "kugou", "酷狗"

    def search(self, kw, n=6):
        # 注意：参数是 keyword（单数）；filter=2 会一条都不给，10 才正常
        q = urllib.parse.urlencode({"keyword": kw, "page": 1, "pagesize": n,
                                    "platform": "WebFilter", "tag": 0, "filter": 10})
        d = _http("http://songsearch.kugou.com/song_search_v2?" + q, headers=self._headers())
        items = ((d.get("data") or {}).get("lists") or [])
        out = []
        for it in items[:n]:
            title = _strip(it.get("SongName"))
            artist = _strip(it.get("SingerName"))
            dur = it.get("Duration") or 0
            out.append({"source": "kugou", "id": it.get("FileHash") or "",
                        "title": title, "artist": artist,
                        "album": it.get("AlbumName") or "",
                        "duration": dur,
                        "album_id": it.get("AlbumID"),
                        "hash": it.get("FileHash") or "",
                        "mixsongid": it.get("MixSongID") or "",
                        "pay": it.get("PayType")})
        return out

    def url(self, song):
        h, aid = song.get("hash") or song.get("id"), song.get("album_id")
        if not h:
            return None, "缺 FileHash"
        q = urllib.parse.urlencode({"hash": h, "album_id": aid or "",
                                    "free_part": 1})
        d = _http("%s/song/url?%s" % (self.base, q), headers=self._headers(), timeout=15)
        if str(d.get("status")) != "1" and d.get("errcode"):
            return None, d.get("error") or ("错误 %s" % d.get("errcode"))
        urls = (((d.get("data") or {}).get("url")) or [])
        if isinstance(urls, str):
            urls = [urls]
        urls = [u for u in urls if u]
        if not urls:
            return None, "没返回直链"
        # /yp/full/ 是完整版，/yp/p_ 是试听，优先完整版
        full = [u for u in urls if "/yp/full/" in u or "/yp/p_" not in u]
        return (full or urls)[0], ""


class NeteaseSource(Source):
    name, label = "netease", "网易云"

    def search(self, kw, n=6):
        q = urllib.parse.urlencode({"keywords": kw, "limit": n})
        d = _http("%s/search?%s" % (self.base, q), headers=self._headers())
        items = (((d.get("result") or {}).get("songs")) or [])
        out = []
        for it in items[:n]:
            ar = it.get("ar") or it.get("artists") or []
            al = it.get("al") or it.get("album") or {}
            out.append({"source": "netease", "id": it.get("id"),
                        "title": it.get("name") or "",
                        "artist": " / ".join(x.get("name") or "" for x in ar),
                        "album": al.get("name") or "",
                        "duration": int((it.get("dt") or it.get("duration") or 0) / 1000),
                        "pay": None})
        return out

    def url(self, song):
        d = _http("%s/song/url?id=%s" % (self.base, song.get("id")),
                  headers=self._headers(), timeout=15)
        for it in (d.get("data") or []):
            if it.get("url"):
                return it["url"], ""
        return None, "网易云没给直链（可能要 VIP）"


class QQSource(Source):
    name, label = "qq", "QQ音乐"

    def search(self, kw, n=6):
        body = json.dumps({"keyword": kw, "num": n, "type": 0}).encode()
        hdr = {"Content-Type": "application/json",
               "User-Agent": UA}
        hdr.update(self._headers())
        r = urllib.request.Request(self.base + "/search/byType", data=body,
                                   headers=hdr)
        with urllib.request.urlopen(r, timeout=10) as resp:
            d = json.loads(resp.read().decode("utf-8", "replace"))
        body_d = d.get("data") or {}
        body_d = body_d.get("body") or body_d
        items = body_d.get("item_song") or []
        if isinstance(items, dict):
            items = items.get("list") or []
        out = []
        for it in items[:n]:
            singers = it.get("singer") or []
            out.append({"source": "qq", "id": it.get("mid") or "",
                        "title": it.get("name") or it.get("title") or "",
                        "artist": " / ".join(s.get("name") or "" for s in singers),
                        "album": (it.get("album") or {}).get("name") or "",
                        "duration": it.get("interval") or 0,
                        "pay": (it.get("pay") or {}).get("pay_play")})
        return out

    def url(self, song):
        q = urllib.parse.urlencode({"mids": song.get("id")})
        d = _http("%s/song/urls?%s" % (self.base, q),
                  headers=self._headers(), timeout=15)
        one = (d.get("data") or {}).get(song.get("id")) or {}
        if one.get("url"):
            return one["url"], ""
        return None, one.get("error") or "QQ 没给直链"


SOURCES = {"kugou": KugouSource, "netease": NeteaseSource, "qq": QQSource}
DEFAULT_PORTS = {"kugou": 4000, "netease": 3000, "qq": 3300}


class Engine(object):
    def __init__(self, cfg):
        m = cfg.get("music") or {}
        host = m.get("host") or "127.0.0.1"
        self.order = m.get("order") or ["kugou", "netease", "qq"]
        self.table = {}
        for key in self.order:
            cls = SOURCES.get(key)
            if not cls:
                continue
            sub = m.get(key) or {}
            if sub.get("enabled", True) is False:
                continue
            self.table[key] = cls(sub.get("host") or host,
                                  sub.get("port") or DEFAULT_PORTS[key])
        self.login = None      # 由 main() 里挂上 login.MusicLogin

    def status(self):
        out = []

        def work(key, s):
            out.append({"name": key, "label": s.label, "up": s.alive()})

        ts = [threading.Thread(target=work, args=(k, s)) for k, s in self.table.items()]
        [t.start() for t in ts]
        [t.join() for t in ts]
        idx = {k: i for i, k in enumerate(self.order)}
        out.sort(key=lambda x: idx.get(x["name"], 99))
        return out

    def search(self, kw, n=6):
        bag = {}

        def work(key, s):
            try:
                if not s.alive():
                    return
                bag[key] = s.search(kw, n)
            except Exception as e:
                log.warning("%s 搜索失败: %s", key, e)

        ts = [threading.Thread(target=work, args=(k, s)) for k, s in self.table.items()]
        [t.start() for t in ts]
        [t.join() for t in ts]
        out = []
        for key in self.order:
            out.extend(bag.get(key) or [])
        for c in out:
            c["score"] = round(_score(c, kw), 1)
        out.sort(key=lambda x: -x["score"])
        return out

    def url(self, song, skip=()):
        """按「先同源、再其它源」取直链，同源失败自动跨源找同名的。"""
        names = [song.get("source")] + [k for k in self.order
                                        if k != song.get("source")]
        why = ""
        for name in names:
            if name in skip:
                continue
            s = self.table.get(name)
            if s is None or not s.alive():
                continue
            cand = song
            if name != song.get("source"):
                hits = [c for c in self._plain_search(song.get("title") or "", name)
                        if _score(c, song.get("title") or "") > 0]
                if not hits:
                    continue
                cand = max(hits, key=lambda c: _score(c, song.get("title") or ""))
            try:
                u, w = s.url(cand)
            except Exception as e:
                u, w = None, str(e)
            if u:
                return {"url": u, "via": name, "song": cand}
            why = "%s: %s" % (s.label, w)
        return {"url": None, "via": None, "song": None, "why": why}

    def _plain_search(self, kw, name):
        s = self.table.get(name)
        if s is None:
            return []
        try:
            return s.search(kw, 5) or []
        except Exception:
            return []


# ---------------------------------------------------------------- 页面
def _read_json(path):
    # PowerShell 的 Out-File 会带 UTF-8 BOM，用 utf-8-sig 才吃得下
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)


def load_cfg(path):
    return _read_json(path)


class Handler(BaseHTTPRequestHandler):
    engine = None
    cfg = None

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionError):
            pass

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        path = u.path

        if path in ("/", "/index.html", "/site.html"):
            with open(os.path.join(HERE, "site.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/app":
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(200, f.read(), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path == "/api/status":
            self._json({"ok": True, "sources": self.engine.status()})
            return
        if path == "/api/search":
            kw = (q.get("q") or [""])[0].strip()
            if not kw:
                self._json({"ok": False, "error": "输入歌名"}, 400)
                return
            n = int((q.get("n") or ["6"])[0])
            try:
                songs = self.engine.search(kw, n)
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 500)
                return
            self._json({"ok": True, "keyword": kw, "songs": songs})
            return
        if path == "/api/stream":
            # 代理音频：浏览器直接 <audio src="/api/stream?..."> 免得跨域/防盗链
            try:
                song = json.loads((q.get("song") or ["{}"])[0])
                skip = set(x for x in (q.get("skip") or [""])[0].split(",") if x)
            except Exception:
                self._json({"ok": False, "error": "参数坏了"}, 400)
                return
            got = self.engine.url(song, skip)
            if not got["url"]:
                self._json({"ok": False, "error": got.get("why") or "取不到直链"}, 502)
                return
            self._proxy(got["url"])
            return
        if path == "/api/resolve":
            try:
                song = json.loads((q.get("song") or ["{}"])[0])
                skip = set(x for x in (q.get("skip") or [""])[0].split(",") if x)
            except Exception:
                self._json({"ok": False, "error": "参数坏了"}, 400)
                return
            self._json(self.engine.url(song, skip))
            return

        # ---------------- 登录相关 ----------------
        L = getattr(self.engine, "login", None)
        if path == "/login" or path == "/api/login/status":
            if L is None:
                self._json({"ok": False, "error": "登录模块未加载"}, 501)
                return
            self._json({"ok": True, "accounts": L.status()})
            return
        if L is not None and path.startswith("/api/login/"):
            rest = path[len("/api/login/"):]
            parts = rest.split("/")
            plat = parts[0] if parts else ""
            act = "/".join(parts[1:]) if len(parts) > 1 else ""
            if plat not in ("kugou", "netease", "qq"):
                self._json({"ok": False, "error": "未知平台"}, 400)
                return
            key = (q.get("key") or [None])[0]
            try:
                if act == "qr":
                    fn = getattr(L, plat + "_qr")
                    self._json(fn())
                elif act == "check":
                    fn = getattr(L, plat + "_check")
                    self._json(fn(key))
                elif act == "logout":
                    self._json(L.logout(plat))
                elif act == "profile":
                    self._json({"ok": True, "profile": L.profile.get(plat)})
                else:
                    self._json({"ok": False, "error": "未知动作"}, 404)
            except Exception as e:
                log.warning("登录 %s/%s 失败: %s", plat, act, e)
                self._json({"ok": False, "error": str(e)}, 500)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    do_POST = do_GET

    def _proxy(self, url):
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": "https://www.kugou.com/",
            "Accept": "*/*",
        })
        try:
            r = urllib.request.urlopen(req, timeout=30)
        except Exception as e:
            self._json({"ok": False, "error": "拉音频失败: %s" % e}, 502)
            return
        ctype = r.headers.get("Content-Type") or "audio/mpeg"
        total = r.headers.get("Content-Length")
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if total:
            self.send_header("Content-Length", total)
        self.send_header("Accept-Ranges", "none")
        self.end_headers()
        try:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionError):
            pass
        finally:
            r.close()


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description="三家音乐源聚合网站")
    ap.add_argument("--host", default="127.0.0.1", help="默认只听本机；对外用 0.0.0.0")
    ap.add_argument("--port", type=int, default=0, help="默认取 config.site.port，再不行 8808")
    ap.add_argument("--cfg", default=os.path.join(HERE, "config.json"))
    a = ap.parse_args()

    cfg = {}
    for p in (a.cfg, os.path.join(HERE, "config.local.json")):
        if os.path.exists(p):
            try:
                cfg.update(_read_json(p))
            except Exception as e:
                log.warning("读配置 %s 失败: %s", p, e)
    Handler.engine = Engine(cfg)
    Handler.cfg = cfg
    try:
        import login as login_mod
        Handler.engine.login = login_mod.MusicLogin(Handler.engine)
        n_in = sum(1 for c in Handler.engine.login.cookies.values() if c)
        log.info("登录模块就绪，已有 %d 个账号登录态", n_in)
    except Exception as e:
        log.warning("登录模块加载失败（不影响搜索）：%s", e)
    site = cfg.get("site") or {}
    port = a.port or int(site.get("port") or 8808)
    srv = ThreadingHTTPServer((a.host, port), Handler)
    srv.daemon_threads = True
    log.info("聚合搜索已启动：http://%s:%d", a.host, port)
    if site.get("domain"):
        log.info("  对外域名：http://%s", site["domain"])
    for st in Handler.engine.status():
        log.info("  %-6s %s", st["label"], "在线" if st["up"] else "连不上")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    sys.exit(main() or 0)
