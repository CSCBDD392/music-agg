# -*- coding: utf-8 -*-
"""三家音乐平台的扫码登录。

设计要点：
1. **登录态只存本地**：cookies 落在 `cookies.json`（.gitignore 已排除），
   绝不进仓库、不进日志。对外只暴露「登没登 / 昵称 / VIP 到期」这类摘要。
2. **登录态注入后端**：拿到的 cookie 以 `Cookie:` 头形式带给上游 API，
   上游本来就认这些 cookie（酷狗 token/userid、网易云 MUSIC_U、QQ 的 qm_keyst）。
3. **不碰音频解密**：只做「用你的账号身份去请求官方接口」，
   拿到什么就是什么（VIP 全曲 or 试听），不破解 DRM。
"""

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

HERE = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(HERE, "cookies.json")

# 每个平台记住哪些 cookie 名（其它一律不存，避免把无用的追踪 cookie 也留下）
KEEP = {
    "kugou":   ("token", "userid", "dfid", "vip_type", "vip_token", "t1",
                "KUGOU_API_MID", "KUGOU_API_GUID", "KUGOU_API_DEV",
                "KUGOU_API_MAC", "KUGOU_API_WEBGL"),
    "netease": ("MUSIC_U", "__csrf", "NMTID"),
    "qq":      ("qm_keyst", "qqmusic_key", "uin", "p_skey", "skey",
                "pt2gguin", "psrf_qqunionid", "euin"),
}


def _req(url, data=None, headers=None, timeout=15):
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    body = None
    if data is not None:
        if isinstance(data, dict):
            body = json.dumps(data).encode()
            h["Content-Type"] = "application/json"
        else:
            body = data
    r = urllib.request.Request(url, data=body, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8", "replace")), resp.headers
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return json.loads(text), e.headers
        except ValueError:
            return {"error": "HTTP %s" % e.code, "body": text[:300]}, e.headers


def _parse_setcookie(headers):
    """把 Set-Cookie 头解析成 dict。"""
    out = {}
    for c in (headers.get_all("Set-Cookie") or []):
        kv = c.split(";")[0]
        if "=" in kv:
            k, v = kv.split("=", 1)
            if v:
                out[k.strip()] = v.strip()
    return out


class MusicLogin(object):
    """三家扫码登录的状态机。线程安全，供 HTTP handler 调用。"""

    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.Lock()
        self.cookies = {"kugou": {}, "netease": {}, "qq": {}}
        self.profile = {"kugou": None, "netease": None, "qq": None}
        self._qr = {}      # 平台 -> {"key":..., "expire":..., "extra":...}
        self.load()

    # ---------------- 持久化 ----------------
    def load(self):
        if not os.path.exists(COOKIE_FILE):
            return
        try:
            with open(COOKIE_FILE, encoding="utf-8") as f:
                d = json.load(f)
            for k in self.cookies:
                self.cookies[k] = d.get("cookies", {}).get(k) or {}
            self.profile = d.get("profile") or self.profile
            # 把盘上的登录态推给上游
            for k in self.cookies:
                self._push(k)
        except Exception as e:
            print("[login] 读 cookies.json 失败: %s" % e)

    def save(self):
        try:
            data = {"cookies": self.cookies, "profile": self.profile,
                    "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
            tmp = COOKIE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, COOKIE_FILE)
        except Exception as e:
            print("[login] 写 cookies.json 失败: %s" % e)

    def cookie_header(self, name):
        c = self.cookies.get(name) or {}
        return "; ".join("%s=%s" % (k, v) for k, v in c.items())

    def _push(self, name):
        """把 cookie 同步给上游 API（每次请求带 Cookie 头即可）。"""
        s = self.engine.table.get(name)
        if s is not None:
            s.cookie = self.cookie_header(name)

    # ---------------- 状态 ----------------
    def status(self):
        out = []
        for name in ("kugou", "netease", "qq"):
            c = self.cookies.get(name) or {}
            p = self.profile.get(name) or {}
            vip = bool(c.get("vip_type") and str(c.get("vip_type")) not in ("0", ""))
            out.append({
                "name": name,
                "logged_in": bool(c),
                "nickname": p.get("nickname") or "",
                "avatar": p.get("avatar") or "",
                "vip": vip or bool(p.get("vip")),
                "vip_desc": p.get("vip_desc") or "",
            })
        return out

    # ================================================================
    #  酷狗
    # ================================================================
    def kugou_qr(self):
        base = self.engine.table["kugou"].base
        d, h = _req(base + "/login/qr/key")
        key = ((d or {}).get("data") or {}).get("qrcode")
        if not key:
            return {"ok": False, "err": "拿不到酷狗二维码: %s" % str(d)[:200]}
        img = (d.get("data") or {}).get("qrcode_img") or ""
        self._qr["kugou"] = {"key": key, "expire": time.time() + 180}
        return {"ok": True, "key": key, "qrimg": img}

    def kugou_check(self, key=None):
        base = self.engine.table["kugou"].base
        # key 优先用前端传来的：服务重启后内存里的 _qr 会丢，但上游那个二维码还有效，
        # 只要 key 还在就能查。绝不能因为"我没印象"就判过期。
        slot = self._qr.get("kugou") or {}
        key = key or slot.get("key")
        if not key:
            return {"ok": False, "state": "expired", "msg": "没有二维码 key，请重新生成"}
        d, h = _req(base + "/login/qr/check?key=" + urllib.parse.quote(key))
        st = ((d or {}).get("data") or {}).get("status")
        jar = _parse_setcookie(h)
        if str(st) == "4":
            data = (d.get("data") or {})
            merged = dict(jar)
            merged.update({k: v for k, v in self._pick("kugou", data).items()})
            picked = self._pick("kugou", merged)
            # 必须真拿到 token 才算登录成功，否则存进去也是个空壳
            if not picked.get("token"):
                return {"ok": True, "state": "wait",
                        "msg": "已收到授权，正在同步账号…"}
            with self._lock:
                self.cookies["kugou"] = picked
                self.profile["kugou"] = {
                    "nickname": data.get("nickname") or data.get("username") or "",
                    "avatar": data.get("pic") or "",
                    "vip": str(data.get("vip_type") or "0") not in ("0", ""),
                    "vip_desc": "VIP" if str(data.get("vip_type") or "0") not in ("0", "") else "",
                }
                self._push("kugou")
                self.save()
            return {"ok": True, "state": "ok", "profile": self.profile["kugou"]}
        if str(st) == "3":
            return {"ok": True, "state": "scanned", "msg": "已扫码，请在手机上点「确认登录」"}
        if str(st) == "2":
            return {"ok": True, "state": "scanned", "msg": "已扫码，等待确认"}
        if str(st) == "0":
            return {"ok": False, "state": "expired", "msg": "二维码已过期，请刷新"}
        return {"ok": True, "state": "wait", "msg": "请用酷狗 App 扫码"}

    # ================================================================
    #  网易云
    # ================================================================
    def netease_qr(self):
        base = self.engine.table["netease"].base
        d, h = _req(base + "/login/qr/key")
        key = ((d or {}).get("data") or {}).get("unikey")
        if not key:
            return {"ok": False, "err": "拿不到网易云二维码: %s" % str(d)[:200]}
        d2, _ = _req(base + "/login/qr/create?key=%s&qrimg=true" % key)
        img = ((d2 or {}).get("data") or {}).get("qrimg") or ""
        self._qr["netease"] = {"key": key, "expire": time.time() + 180,
                               "jar": _parse_setcookie(h)}
        return {"ok": True, "key": key, "qrimg": img}

    def netease_check(self, key=None):
        base = self.engine.table["netease"].base
        slot = self._qr.get("netease") or {}
        key = key or slot.get("key")
        if not key:
            return {"ok": False, "state": "expired", "msg": "没有二维码 key，请重新生成"}
        jar = dict(slot.get("jar") or {})
        cookie = "; ".join("%s=%s" % (k, v) for k, v in jar.items())
        d, h = _req(base + "/login/qr/check?key=" + key, headers={"Cookie": cookie})
        jar.update(_parse_setcookie(h))
        code = str((d or {}).get("code"))
        if code == "803":                       # 授权成功，cookie 在 cookie 字段
            ck = (d or {}).get("cookie") or ""
            got = _parse_cookie_str(ck)
            jar.update(got)
            # 关键：上游有时会回 803 但 cookie 里没有 MUSIC_U（匿名态/空授权），
            # 这种不能当登录成功，否则会存一个匿名账号，前端看着像"授权了但没反应"。
            if not got.get("MUSIC_U"):
                return {"ok": True, "state": "wait",
                        "msg": "已收到授权，正在同步账号…"}
            with self._lock:
                self.cookies["netease"] = self._pick("netease", jar)
                self._push("netease")
                self.save()
            prof = self._netease_profile()
            if not prof.get("nickname"):
                return {"ok": True, "state": "wait",
                        "msg": "已授权，正在读取账号信息…"}
            self.profile["netease"] = prof
            self.save()
            return {"ok": True, "state": "ok", "profile": prof}
        if code == "802":
            return {"ok": True, "state": "scanned", "msg": "已扫码，请在手机上确认"}
        if code == "800":
            return {"ok": False, "state": "expired", "msg": "二维码已过期，请刷新"}
        return {"ok": True, "state": "wait", "msg": "请用网易云音乐 App 扫码"}

    def _netease_profile(self):
        base = self.engine.table["netease"].base
        ck = self.cookie_header("netease")
        d, _ = _req(base + "/login/status", headers={"Cookie": ck})
        data = ((d or {}).get("data") or {})
        acc = data.get("account") or {}
        prof = data.get("profile") or {}
        vip = (data.get("profile") or {}).get("vipType") or 0
        return {"nickname": prof.get("nickname") or acc.get("userName") or "",
                "avatar": prof.get("avatarUrl") or "",
                "vip": bool(vip),
                "vip_desc": "黑胶VIP" if vip else ""}

    # ================================================================
    #  QQ 音乐
    # ================================================================
    def qq_qr(self):
        base = self.engine.table["qq"].base
        d, h = _req(base + "/login/qrcode", data={})
        data = (d or {}).get("data") or {}
        img = data.get("data") or ""
        ident = data.get("identifier") or ""
        if not img:
            return {"ok": False, "err": "拿不到 QQ 二维码: %s" % str(d)[:200]}
        mime = data.get("mime") or "image/png"
        if not img.startswith("data:"):
            img = "data:%s;base64,%s" % (mime, img)
        self._qr["qq"] = {"key": ident, "expire": time.time() + 180,
                          "mime": mime, "jar": _parse_setcookie(h)}
        return {"ok": True, "key": ident, "qrimg": img}

    def qq_check(self, key=None):
        base = self.engine.table["qq"].base
        slot = self._qr.get("qq") or {}
        ident = key or slot.get("key")
        if not ident:
            return {"ok": False, "state": "expired", "msg": "二维码已失效，请刷新"}
        jar = dict(slot.get("jar") or {})
        d, h = _req(base + "/login/checkQrcode",
                    data={"identifier": ident,
                          "ptqrtoken": slot.get("ptqrtoken") or "",
                          "pt_login_sig": slot.get("pt_login_sig") or "",
                          "cookie": "; ".join("%s=%s" % (k, v) for k, v in jar.items())})
        jar.update(_parse_setcookie(h))
        code = (d or {}).get("code")
        data = (d or {}).get("data") or {}
        if code == 0 and (data.get("cookies") or data.get("cookie")):
            merged = dict(jar)
            merged.update(_parse_cookie_str(data.get("cookies") or data.get("cookie") or ""))
            merged.update(data.get("cookies") or {})
            picked = self._pick("qq", merged)
            # 同样要校验：没拿到真身份 cookie 就不能算登录成功
            if not any(k in picked for k in ("qm_keyst", "qqmusic_key", "p_skey")):
                return {"ok": True, "state": "wait",
                        "msg": "已收到授权，正在同步账号…"}
            with self._lock:
                self.cookies["qq"] = picked
                self._push("qq")
                self.save()
            return {"ok": True, "state": "ok", "profile": self.profile.get("qq")}
        msg = str(data.get("msg") or (d or {}).get("msg") or "")
        if "已扫码" in msg or "确认" in msg:
            return {"ok": True, "state": "scanned", "msg": msg}
        if "过期" in msg or "失效" in msg:
            return {"ok": False, "state": "expired", "msg": msg}
        return {"ok": True, "state": "wait", "msg": msg or "请用 QQ 扫码"}

    # ================================================================
    def _pick(self, name, jar):
        keep = KEEP.get(name) or ()
        return {k: v for k, v in (jar or {}).items() if k in keep and v}

    def logout(self, name):
        with self._lock:
            self.cookies[name] = {}
            self.profile[name] = None
            self._push(name)
            self.save()
        return {"ok": True}


def _parse_cookie_str(s):
    """解析 cookie 字符串，兼容三种坑：

    1. 普通 `a=1; b=2`
    2. Set-Cookie 拼接式 `a=1; Max-Age=999; Path=/;;b=2; Path=/`
       这里 `Max-Age/Expires/Path/Domain/Secure/HttpOnly/SameSite` 是属性不是 cookie，
       必须丢掉，否则 `Path=/api/clientlog` 会被当成 `Path` 这个 cookie。
    3. 属性结尾没分号再跟下一个 cookie（网易云就是这种）

    网易云返回的 MUSIC_U 排在 4000 多字符之后，上面的解析一旦错位就整串丢光，
    所以这里必须逐段判断，宁可多切也不能少切。
    """
    ATTRS = ("max-age", "expires", "path", "domain", "secure", "httponly",
             "samesite", "version", "comment", "priority")
    out = {}
    for part in (s or "").replace("\n", ";").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if not k or k.lower() in ATTRS:
            continue
        # 值里可能还粘着 "Expires=Tue, 09 Nov..." 之类，取第一段
        if "," in v and k.lower() not in ("expires",):
            v = v.split(",")[0].strip()
        if v:
            out[k] = v
    return out
