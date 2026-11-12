#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第三方哔哩哔哩客户端 · PC 版（Python，Windows / Linux 通用）

架构说明：
  本程序 = 本地 HTTP 服务 + 浏览器应用窗口。
  - 本地服务负责：B 站接口代理（游客身份 / 防封限速）、视频直链解析、
    视频分段中转（补齐 Referer 与 UA）、弹幕 XML 下发。
  - 应用窗口负责：界面渲染，内置 <video> 播放器 + 自绘弹幕层。

  播放不依赖 B 站网页播放器，也不调用 mpv / ffplay 等外部程序，
  Windows 与 Linux 行为一致，且无需任何第三方依赖。
"""

import gzip
import hashlib
import http.cookiejar
import json
import os
import platform
import random
import re
import base64
import socket
import struct
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

APP_NAME = "第三方哔哩哔哩客户端"
APP_VERSION = "1.9.2"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UI_PATH = os.path.join(BASE_DIR, "app", "ui.html")
HOME = os.path.expanduser("~")
STATE_DIR = os.path.join(HOME, ".msz-bili-pc")
STATE_FILE = os.path.join(STATE_DIR, "state.json")


# ----------------------------------------------------------------------------
# 本地状态
# ----------------------------------------------------------------------------
def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(st):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# 会话：CookieJar 保存浏览器指纹
# ----------------------------------------------------------------------------
class Session(object):
    def __init__(self):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))
        self.lock = threading.Lock()

    def warm(self):
        if any(c.name == "buvid3" for c in self.jar):
            return True
        st, _ = http_get("https://www.bilibili.com/", timeout=10)
        return st in (200, 412)


SESS = Session()


def load_cookies():
    """启动时把上次登录的 Cookie 灌回 CookieJar。"""
    st = load_state()
    for c in st.get("cookies") or []:
        try:
            ck = http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False, c.get("domain", ".bilibili.com"),
                True, False, c.get("path", "/"), True, False, None, False, None, None, {})
            SESS.jar.set_cookie(ck)
        except Exception:
            pass


def save_cookies():
    cookies = []
    for c in SESS.jar:
        if not c.value:
            continue
        cookies.append({"name": c.name, "value": c.value,
                        "domain": c.domain, "path": c.path or "/"})
    st = load_state()
    st["cookies"] = cookies
    save_state(st)


def cookie_dict():
    d = {}
    for c in SESS.jar:
        if c.value:
            d[c.name] = c.value
    return d


def cookie_string():
    parts = []
    for c in SESS.jar:
        if c.value and ("bilibili" in (c.domain or "")):
            parts.append("%s=%s" % (c.name, c.value))
    return "; ".join(parts)


def _decode(data, encoding):
    enc = (encoding or "").lower()
    try:
        if enc == "gzip":
            return gzip.decompress(data)
        if enc == "deflate":
            try:
                return zlib.decompress(data)
            except Exception:
                return zlib.decompress(data, -15)
    except Exception:
        return data
    return data


def http_get(url, referer="https://www.bilibili.com/", timeout=12):
    url = urllib.parse.quote(url, safe=":/?&=%#[]!$'()*+,;=@~")
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Origin": "https://www.bilibili.com",
        "Referer": referer,
        "Accept-Encoding": "gzip, deflate",
    })
    ck = cookie_string()
    if ck:
        req.add_header("Cookie", ck)
    try:
        with SESS.opener.open(req, timeout=timeout) as r:
            data = _decode(r.read(), r.headers.get("Content-Encoding"))
            return r.status, data.decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            data = _decode(e.read(), e.headers.get("Content-Encoding"))
            return e.code, data.decode("utf-8", "replace")
        except Exception:
            return e.code, ""
    except Exception as e:
        return 0, "请求失败：%s" % e


# ----------------------------------------------------------------------------
# 游客身份
# ----------------------------------------------------------------------------
def _random_hex(n):
    return "".join(random.choice("0123456789ABCDEF") for _ in range(n))


def _fake_buvid():
    s = _random_hex(32)
    return "%s-%s-%s-%s-%s" % (s[0:8], s[8:12], s[12:16], s[16:20], s[20:32])


def ensure_guest():
    st = load_state()
    g = st.get("guest") or {}
    if g.get("buvid3"):
        return g

    SESS.warm()
    status, body = http_get("https://api.bilibili.com/x/frontend/finger/spi")
    b3 = b4 = ""
    if status == 200:
        try:
            d = json.loads(body).get("data") or {}
            b3 = d.get("b_3") or ""
            b4 = d.get("b_4") or ""
        except Exception:
            pass
    g = {
        "buvid3": b3 or _fake_buvid(),
        "buvid4": b4 or _fake_buvid(),
        "name": "游客 " + _random_hex(6),
        "created": int(time.time()),
    }
    st["guest"] = g
    save_state(st)
    return g


# ----------------------------------------------------------------------------
# 防封
# ----------------------------------------------------------------------------
class Guard(object):
    MIN_GAP = 0.9
    MAX_GAP = 1.4
    PER_MINUTE = 30
    COOLDOWN = 300

    def __init__(self):
        self.lock = threading.Lock()
        self.last = 0.0
        self.stamps = []
        self.cool_until = 0.0
        self.blocked_writes = 0

    def wait_turn(self):
        with self.lock:
            now = time.time()
            if now < self.cool_until:
                raise RuntimeError("触发风控冷却中，请 %d 秒后再试" % int(self.cool_until - now))
            gap = random.uniform(self.MIN_GAP, self.MAX_GAP)
            sleep_for = max(0.0, self.last + gap - now)
            self.last = now + sleep_for
            self.stamps = [t for t in self.stamps if now - t < 60]
            if len(self.stamps) >= self.PER_MINUTE:
                raise RuntimeError("请求过于频繁，已本地限流，请稍后重试")
            self.stamps.append(now)
        if sleep_for > 0:
            time.sleep(sleep_for)

    def trigger_cooldown(self):
        with self.lock:
            self.cool_until = time.time() + self.COOLDOWN


GUARD = Guard()
GUEST = None

WRITE_HINTS = ("/x/v2/reply/add", "/x/web-interface/coin/add", "/x/web-interface/archive/like",
               "/x/v3/fav/resource/deal", "/x/dynamic/feed/create/dyn", "/x/article/create")

STREAM_CACHE = {}
STREAM_LOCK = threading.Lock()


def referer_for(path):
    if "search/type" in path:
        return "https://search.bilibili.com/"
    return "https://www.bilibili.com/"


DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "MSZ-Bili-PC")
_DL = {}

_ILLEGAL = '<>:"/\\|?*\r\n\t'


def _safe_name(name):
    name = "".join("_" if (ch in _ILLEGAL or ord(ch) < 32) else ch for ch in name)
    name = name.strip().strip(".") or "video"
    return name[:80]


def start_download(bvid, qn="64", title=""):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    qn = str(qn) if str(qn).isdigit() else "64"
    info = resolve_stream(bvid, qn)
    url = info.get("url") or ""
    if not url:
        raise RuntimeError("取不到直链，可能该视频需要登录或已下架")
    name = "%s [%s].mp4" % (_safe_name(title or bvid), bvid)
    path = os.path.join(DOWNLOAD_DIR, name)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return {"name": name, "path": path, "skip": True}

    def run():
        tmp = path + ".part"
        req = urllib.request.Request(url, headers={
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
            "Accept-Encoding": "identity",
            "Range": "bytes=0-",
        })
        try:
            with SESS.opener.open(req, timeout=30) as r, open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1024 * 256)
                    if not chunk:
                        break
                    f.write(chunk)
            os.replace(tmp, path)
            _DL[name] = {"state": "done", "path": path}
        except Exception as e:
            _DL[name] = {"state": "error", "error": str(e)}
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass

    _DL[name] = {"state": "running", "path": path}
    th = threading.Thread(target=run, daemon=True)
    th.start()
    return {"name": name, "path": path, "skip": False}


def list_downloads():
    out = []
    if not os.path.isdir(DOWNLOAD_DIR):
        return out
    for fn in sorted(os.listdir(DOWNLOAD_DIR)):
        fp = os.path.join(DOWNLOAD_DIR, fn)
        if not os.path.isfile(fp) or fn.endswith(".part"):
            continue
        info = _DL.get(fn) or {}
        out.append({"name": fn, "path": fp, "size": os.path.getsize(fp),
                    "state": info.get("state", "idle")})
    return out


# ----------------------------------------------------------------------------
# 投稿（创作中心上传）
# ----------------------------------------------------------------------------
MEMBER = "https://member.bilibili.com"
UP_TMP = os.path.join(os.path.expanduser("~"), ".msz-bili-pc", "upload")
UP_STATE = {}

REGION_TIDS = [
    ("动画", 1), ("番剧", 13), ("国创", 181), ("音乐", 3), ("舞蹈", 129),
    ("游戏", 4), ("知识", 36), ("科技", 188), ("运动", 234), ("生活", 160),
    ("美食", 211), ("动物圈", 217), ("鬼畜", 119), ("时尚", 155),
    ("资讯", 202), ("娱乐", 5), ("影视", 181), ("纪录片", 37),
]


def _member_post(path, params=None, json_body=None, extra_headers=None):
    dt = cookie_dict()
    csrf = dt.get("bili_jct", "")
    if not csrf:
        raise RuntimeError("需要登录后才能投稿")
    url = MEMBER + path
    if params is not None:
        params = dict(params)
        params["csrf"] = csrf
        body = urllib.parse.urlencode(params).encode("utf-8")
        ctype = "application/x-www-form-urlencoded"
    else:
        body = json.dumps(json_body or {}).encode("utf-8")
        ctype = "application/json"
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Referer", "https://member.bilibili.com/platform/upload/video/frame")
    req.add_header("Origin", "https://member.bilibili.com")
    req.add_header("Content-Type", ctype)
    ck = cookie_string()
    if ck:
        req.add_header("Cookie", ck)
    for k, v in (extra_headers or {}).items():
        req.add_header(k, v)
    with SESS.opener.open(req, timeout=60) as r:
        return json.loads(_decode(r.read(), r.headers.get("Content-Encoding")).decode("utf-8", "replace"))


def _member_upload_part(token, partnum, filename, data):
    boundary = "----MSZBILI" + hashlib.md5(str(time.time()).encode()).hexdigest()
    head = ("--%s\r\nContent-Disposition: form-data; name=\"part\"; filename=\"%s\"\r\n"
            "Content-Type: application/octet-stream\r\n\r\n" % (boundary, filename)).encode("utf-8")
    tail = ("\r\n--%s--\r\n" % boundary).encode("utf-8")
    body = head + data + tail
    url = (MEMBER + "/x/vu/web/video/part/upload?upload_token=%s&partnum=%s"
           "&csrf=%s" % (urllib.parse.quote(str(token)), partnum,
                         urllib.parse.quote(cookie_dict().get("bili_jct", ""), safe="")))
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("User-Agent", UA)
    req.add_header("Referer", "https://member.bilibili.com/platform/upload/video/frame")
    req.add_header("Content-Type", "multipart/form-data; boundary=%s" % boundary)
    ck = cookie_string()
    if ck:
        req.add_header("Cookie", ck)
    with SESS.opener.open(req, timeout=120) as r:
        return json.loads(_decode(r.read(), r.headers.get("Content-Encoding")).decode("utf-8", "replace"))


def _upos_request(url, method="POST", data=None, headers=None, timeout=30):
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("User-Agent", UA)
    req.add_header("Referer", "https://member.bilibili.com/platform/upload/video/frame")
    req.add_header("Origin", "https://member.bilibili.com")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    ck = cookie_string()
    if ck:
        req.add_header("Cookie", ck)
    with SESS.opener.open(req, timeout=timeout) as r:
        return r.status, r.read()


def submit_video(job_id, filename, title, desc, tag, tid, copyright_type, source):
    st = UP_STATE.setdefault(job_id, {"log": [], "done": False, "ok": False, "bvid": ""})

    def log(msg):
        st["log"].append(msg)

    try:
        dt = cookie_dict()
        if not dt.get("SESSDATA"):
            log("× 未登录：投稿需要先扫码登录客户端")
            st["done"] = True
            return
        size = os.path.getsize(filename)
        name = os.path.basename(filename)
        log("① 预上传：%s（%.1f MB）" % (name, size / 1048576.0))

        q = urllib.parse.urlencode({
            "upcdn": "bda2", "probe_version": "20221109", "r": "upos",
            "profile": "ugcupos/bup", "ssl": "0", "version": "2.8.12",
            "build": "2081200", "name": name, "size": str(size),
        })
        code, body = http_get("https://member.bilibili.com/preupload?" + q, timeout=20)
        if code != 200:
            log("× 预上传被拒（HTTP %s）：登录态没生效或被风控" % code)
            st["done"] = True
            return
        try:
            ret = json.loads(body)
        except Exception:
            log("× 预上传返回异常：%s" % body[:120])
            st["done"] = True
            return
        if ret.get("OK") != 1:
            log("× 预上传失败：%s" % json.dumps(ret)[:160])
            st["done"] = True
            return

        endpoint = ret.get("endpoint") or ""
        upos_uri = ret.get("upos_uri") or ""
        auth = ret.get("auth") or ""
        biz_id = ret.get("biz_id")
        chunk_size = int(ret.get("chunk_size") or (8 * 1024 * 1024))
        base = "https:" + endpoint + "/" + upos_uri.replace("upos://", "")
        hdr = {"X-Upos-Auth": auth}
        log("② 拿到上传通道，分片大小 %.1f MB" % (chunk_size / 1048576.0))

        code, body = _upos_request(base + "?uploads&output=json", headers=hdr, timeout=30)
        try:
            upload_id = json.loads(body).get("upload_id")
        except Exception:
            upload_id = None
        if not upload_id:
            log("× 创建上传任务失败：%s" % (body[:120]))
            st["done"] = True
            return

        total = max(1, (size + chunk_size - 1) // chunk_size)
        parts = []
        with open(filename, "rb") as f:
            for i in range(total):
                f.seek(i * chunk_size)
                blob = f.read(chunk_size)
                u = "%s?uploadId=%s&partNumber=%d&output=json" % (base, upload_id, i + 1)
                code, _ = _upos_request(u, method="PUT", data=blob, headers=hdr, timeout=120)
                if code not in (200, 201):
                    log("× 分片 %d/%d 上传失败（HTTP %s）" % (i + 1, total, code))
                    st["done"] = True
                    return
                parts.append({"partNumber": i + 1, "eTag": "etag"})
                log("   分片 %d/%d 已上传" % (i + 1, total))

        merge_q = urllib.parse.urlencode({"name": name, "uploadId": upload_id,
                                          "biz_id": biz_id or "", "output": "json",
                                          "profile": "ugcupos/bup"})
        code, body = _upos_request(base + "?" + merge_q, data=json.dumps({"parts": parts}).encode(),
                                   headers=dict(hdr, **{"Content-Type": "application/json"}), timeout=60)
        try:
            ok = json.loads(body).get("OK") == 1
        except Exception:
            ok = False
        if not ok:
            log("× 合并分片失败：%s" % body[:160])
            st["done"] = True
            return
        log("③ 视频上传完成，正在提交稿件…")

        token = os.path.splitext(os.path.basename(upos_uri))[0]
        payload = {
            "copyright": int(copyright_type or 1),
            "source": source or "",
            "tid": int(tid or 171),
            "cover": "",
            "title": title or os.path.splitext(name)[0],
            "desc_format_id": 0,
            "desc": desc or "",
            "dynamic": "",
            "tag": tag or "原创",
            "videos": [{"filename": token, "title": title or "", "desc": desc or ""}],
            "subtitle": {"open": 0, "lan": ""},
            "no_reprint": 0,
        }
        r3 = _member_post("/x/vu/web/add", json_body=payload)
        if r3.get("code") != 0:
            log("× 提交失败：%s" % (r3.get("message") or r3.get("code")))
            st["done"] = True
            return
        bvid = ((r3.get("data") or {}).get("bvid")) or ""
        st["bvid"] = bvid
        st["ok"] = True
        log("✓ 投稿已提交，稿件号：%s" % (bvid or "（审核中）"))
    except Exception as e:
        log("× 出错：%s" % e)
    finally:
        st["done"] = True


_LOGIN_IMPORT = {"t": 0.0}


def try_import_browser_login():
    """客户端没登录但浏览器（投稿页）登录了：自动把登录态拉回来。"""
    if cookie_dict().get("SESSDATA"):
        return False
    if not CDP_PORT:
        return False
    now = time.time()
    if now - _LOGIN_IMPORT["t"] < 12:
        return False
    _LOGIN_IMPORT["t"] = now
    try:
        return pull_cookies_from_browser() > 0
    except Exception:
        return False


def proxy(path):
    if not (path.startswith("/x/") or path.startswith("/pgc/")):
        return 400, {"code": -1, "message": "不支持的接口路径"}
    try_import_browser_login()
    for w in WRITE_HINTS:
        if w in path:
            GUARD.blocked_writes += 1
            return 200, {"code": -1, "message": "游客模式为只读，写操作已在本地拦截"}

    GUARD.wait_turn()
    SESS.warm()
    status, body = http_get("https://api.bilibili.com" + path, referer=referer_for(path))
    if status == 412:
        GUARD.trigger_cooldown()
        return 412, {"code": -412, "message": "触发 B 站风控（412），已自动冷却 5 分钟"}
    if status == 0:
        return 502, {"code": -502, "message": body or "网络不可达，请检查网络连接"}
    try:
        return status, json.loads(body)
    except Exception:
        return 502, {"code": -502, "message": "上游返回非 JSON（HTTP %d）" % status}


# ----------------------------------------------------------------------------
# WBI 签名（空间主页 / 专栏等接口需要）
# ----------------------------------------------------------------------------
MIXIN_TABLE = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
]
_WBI_CACHE = {"t": 0, "mixin": ""}

# 空间接口的风控指纹参数，缺一项就回 -352
SPACE_EXTRA = {
    "dm_img_list": "[]",
    "dm_img_str": "V2ViR0wgMS4wIChPcGVuR0wgRVMgMi4wIENocm9taXVtKQ",
    "dm_cover_img_str": "QU5HS0xFLCBOVklESUEgQ09SUE9SQVRJT04sIElOVEVMIElSSVMgWDU1MCwgTlZJRElBIEdFRk9SQ0UgR1RYIDE2NTAsIERJUkVDM0QgMTIuMA",
    "dm_img_inter": '{"ds":[],"wh":[0,0,0],"of":[0,0,0]}',
}


def wbi_mixin():
    now = time.time()
    if _WBI_CACHE["mixin"] and now - _WBI_CACHE["t"] < 3600:
        return _WBI_CACHE["mixin"]
    _, body = http_get("https://api.bilibili.com/x/web-interface/nav")
    try:
        d = json.loads(body)["data"]["wbi_img"]
        img = d["img_url"].rsplit("/", 1)[-1].split(".")[0]
        sub = d["sub_url"].rsplit("/", 1)[-1].split(".")[0]
        raw = img + sub
        mixin = "".join(raw[i] for i in MIXIN_TABLE[:32])
    except Exception:
        mixin = ""
    _WBI_CACHE["mixin"] = mixin
    _WBI_CACHE["t"] = now
    return mixin


def wbi_sign(params):
    mixin = wbi_mixin()
    params = dict(params)
    params["wts"] = str(int(time.time()))
    items = []
    for k in sorted(params.keys()):
        v = str(params[k])
        v = re.sub(r"[!'()*]", "", v)
        items.append((k, v))
    query = urllib.parse.urlencode(items)
    w = hashlib.md5((query + mixin).encode("utf-8")).hexdigest()
    return query + "&w_rid=" + w


def api_wbi(path, params):
    GUARD.wait_turn()
    SESS.warm()
    url = "https://api.bilibili.com" + path + "?" + wbi_sign(params)
    status, body = http_get(url)
    try:
        return status, json.loads(body)
    except Exception:
        return 502, {"code": -502, "message": "上游返回非 JSON（HTTP %s）" % status}


# ----------------------------------------------------------------------------
# 播放直链解析
# ----------------------------------------------------------------------------
def resolve_stream(bvid, qn=80, ep=""):
    now = time.time()
    key = "%s@%s@%s" % (bvid, qn, ep)
    with STREAM_LOCK:
        hit = STREAM_CACHE.get(key)
        if hit and now - hit[0] < 180:
            return hit[1]

    GUARD.wait_turn()
    SESS.warm()

    # 番剧/影视：优先走 PGC 播放接口
    if ep.isdigit():
        pg = json.loads(http_get(
            "https://api.bilibili.com/pgc/player/web/playurl?ep_id=%s&qn=%s&fnval=0&fourk=1&avid=0&cid=0"
            % (ep, qn), referer="https://www.bilibili.com/bangumi/play/ep%s" % ep)[1])
        res = pg.get("result") or {}
        durl = (res.get("durl") or [{}])[0]
        if pg.get("code") == 0 and durl.get("url"):
            result = {
                "title": res.get("title") or ("ep%s" % ep),
                "qn": res.get("quality"),
                "accept_quality": res.get("accept_quality") or [],
                "accept_desc": res.get("accept_description") or [],
                "aid": res.get("aid") or 0,
                "cid": res.get("cid") or 0,
                "url": durl.get("url") or "",
                "quality": res.get("quality"),
                "format": res.get("format"),
                "length": 0,
                "is_preview": res.get("is_preview"),
                "ep": ep,
            }
            with STREAM_LOCK:
                STREAM_CACHE[key] = (now, result)
            return result

    info = json.loads(http_get("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid,
                               referer="https://www.bilibili.com/video/%s" % bvid)[1])
    if info.get("code") != 0:
        raise RuntimeError("获取视频信息失败：%s" % info.get("message"))
    d = info["data"]
    aid, cid, title = d.get("aid"), d.get("cid"), d.get("title")

    time.sleep(random.uniform(GUARD.MIN_GAP, GUARD.MAX_GAP))
    q = json.loads(http_get(
        "https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=%s&fnval=0&fourk=1"
        % (bvid, cid, qn), referer="https://www.bilibili.com/video/%s" % bvid)[1])
    if q.get("code") != 0:
        raise RuntimeError("获取播放地址失败：%s" % q.get("message"))
    pd = q["data"]
    durl = (pd.get("durl") or [{}])[0]

    result = {
        "title": title,
        "qn": pd.get("quality"),
        "accept_quality": pd.get("accept_quality") or [],
        "accept_desc": pd.get("accept_description") or [],
        "aid": aid,
        "cid": cid,
        "url": durl.get("url") or "",
        "quality": pd.get("quality"),
        "format": pd.get("format"),
        "length": d.get("duration"),
    }
    if not result["url"]:
        raise RuntimeError("未取到播放地址（可能受地区限制或需要登录）")
    with STREAM_LOCK:
        STREAM_CACHE[key] = (now, result)
    return result


# ----------------------------------------------------------------------------
# 分段中转
# ----------------------------------------------------------------------------
def open_upstream(url, range_header=None):
    headers = {
        "User-Agent": UA,
        "Referer": "https://www.bilibili.com/",
        "Accept": "*/*",
        "Accept-Encoding": "identity",
    }
    if range_header:
        headers["Range"] = range_header
    ck = cookie_string()
    if ck:
        headers["Cookie"] = ck
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20)


# ----------------------------------------------------------------------------
# HTTP 服务
# ----------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "MSZBiliPC"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except Exception:
            pass

    def _serve_stream(self, bvid, qn="80", ep=""):
        try:
            st = resolve_stream(bvid, qn, ep)
        except Exception as e:
            self._send(502, {"code": -502, "message": str(e)})
            return
        rng = self.headers.get("Range")
        try:
            up = open_upstream(st["url"], rng)
        except Exception as e:
            self._send(502, {"code": -502, "message": "取流失败：%s" % e})
            return

        try:
            self.send_response(206 if up.status == 206 else 200)
            self.send_header("Content-Type", "video/mp4")
            self.send_header("Accept-Ranges", "bytes")
            if up.headers.get("Content-Range"):
                self.send_header("Content-Range", up.headers["Content-Range"])
            if up.headers.get("Content-Length"):
                self.send_header("Content-Length", up.headers["Content-Length"])
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = up.read(65536)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except Exception:
            pass
        finally:
            try:
                up.close()
            except Exception:
                pass

    def _serve_danmaku(self, bvid, ep=""):
        try:
            cid = (resolve_stream(bvid, "64", ep) or {}).get("cid") or 0
            if not cid:
                raise RuntimeError("no cid")
        except Exception:
            self._send(200, "<?xml version=\"1.0\"?><i></i>", "text/xml; charset=utf-8")
            return
        _, body = http_get("https://comment.bilibili.com/%s.xml" % cid,
                           referer="https://www.bilibili.com/video/%s" % bvid)
        self._send(200, body, "text/xml; charset=utf-8")

    def _handle_upload(self):
        dt = cookie_dict()
        if not dt.get("bili_jct"):
            self._send(200, {"code": -101, "message": "投稿需要登录，请先扫码登录"})
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0 or n > 4 * 1024 * 1024 * 1024:
            self._send(400, {"code": -1, "message": "文件为空或超过 4GB"})
            return
        raw = self.rfile.read(n)
        ctype = self.headers.get("Content-Type") or ""
        if "boundary=" not in ctype:
            self._send(400, {"code": -1, "message": "上传格式错误"})
            return
        boundary = ctype.split("boundary=")[-1].strip().encode("utf-8")
        fields, filepath, fname = {}, None, "video.mp4"
        os.makedirs(UP_TMP, exist_ok=True)
        for part in raw.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            hd, body = part.split(b"\r\n\r\n", 1)
            body = body.rstrip(b"\r\n")
            if b"filename=" in hd:
                try:
                    fname = hd.split(b"filename=\"")[1].split(b"\"")[0].decode("utf-8", "replace")
                except Exception:
                    pass
                filepath = os.path.join(UP_TMP, "up_" + str(int(time.time())) + os.path.splitext(fname)[1])
                with open(filepath, "wb") as f:
                    f.write(body)
            else:
                try:
                    key = hd.split(b'name="')[1].split(b'"')[0].decode("utf-8", "replace")
                except Exception:
                    continue
                try:
                    fields[key] = body.decode("utf-8", "replace")
                except Exception:
                    fields[key] = ""
        if not filepath or not os.path.exists(filepath):
            self._send(400, {"code": -1, "message": "没有收到视频文件"})
            return
        job_id = hashlib.md5((str(time.time()) + fname).encode()).hexdigest()[:12]
        UP_STATE[job_id] = {"log": ["已收到文件 %s" % fname], "done": False, "ok": False, "bvid": ""}
        th = threading.Thread(target=submit_video, args=(
            job_id, filepath, fields.get("title", ""), fields.get("desc", ""),
            fields.get("tag", ""), fields.get("tid", "171"),
            fields.get("copyright", "1"), fields.get("source", "")), daemon=True)
        th.start()
        self._send(200, {"code": 0, "data": {"id": job_id}})

    def _read_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except Exception:
            n = 0
        if n <= 0:
            return {}
        raw = self.rfile.read(n).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return dict(urllib.parse.parse_qsl(raw))

    def do_POST(self):
        try:
            try_import_browser_login()
        except Exception:
            pass
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/draft/save":
            body = self._read_body()
            st = load_state()
            d = st.get("drafts") or []
            d.insert(0, {"title": body.get("title", ""), "desc": body.get("desc", ""),
                         "tag": body.get("tag", ""), "tid": body.get("tid", ""),
                         "file": body.get("file", ""), "at": time.strftime("%Y-%m-%d %H:%M")})
            st["drafts"] = d[:50]
            save_state(st)
            self._send(200, {"code": 0, "message": "草稿已保存"})
            return

        if parsed.path == "/upload":
            self._handle_upload()
            return

        if parsed.path != "/post":
            self._send(404, {"code": -1, "message": "not found"})
            return

        qs = urllib.parse.parse_qs(parsed.query)
        p = (qs.get("p") or [""])[0]
        if not (p.startswith("/x/") or p.startswith("/pgc/")):
            self._send(400, {"code": -1, "message": "不支持的接口路径"})
            return

        dt = cookie_dict()
        csrf = dt.get("bili_jct", "")
        if not csrf:
            self._send(200, {"code": -101, "message": "需要登录后才能操作（点赞/投币/关注/发弹幕）"})
            return

        data = self._read_body()
        data["csrf"] = csrf
        data["csrf_token"] = csrf

        try:
            GUARD.wait_turn()
        except RuntimeError as e:
            self._send(429, {"code": -429, "message": str(e)})
            return

        body = urllib.parse.urlencode(data).encode("utf-8")
        req = urllib.request.Request("https://api.bilibili.com" + p, data=body, method="POST")
        req.add_header("User-Agent", UA)
        req.add_header("Referer", "https://www.bilibili.com/")
        req.add_header("Origin", "https://www.bilibili.com")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        ck = cookie_string()
        if ck:
            req.add_header("Cookie", ck)
        try:
            with SESS.opener.open(req, timeout=15) as r:
                raw = _decode(r.read(), r.headers.get("Content-Encoding"))
                self._send(200, json.loads(raw.decode("utf-8", "replace")))
        except urllib.error.HTTPError as e:
            self._send(502, {"code": e.code, "message": "上游 HTTP %s" % e.code})
        except Exception as e:
            self._send(502, {"code": -502, "message": "写操作失败：%s" % e})

    def do_GET(self):
        try:
            try_import_browser_login()
        except Exception:
            pass
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if parsed.path in ("/", "/index.html"):
            try:
                with open(UI_PATH, "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception as e:
                self._send(500, "界面文件缺失：%s" % e, "text/plain; charset=utf-8")
            return

        if parsed.path == "/hls.min.js":
            try:
                with open(os.path.join(BASE_DIR, "app", "hls.min.js"), "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except Exception:
                self._send(404, "not found", "text/plain; charset=utf-8")
            return

        if parsed.path == "/qrcode.min.js":
            try:
                with open(os.path.join(BASE_DIR, "app", "qrcode.min.js"), "r", encoding="utf-8") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except Exception:
                self._send(404, "not found", "text/plain; charset=utf-8")
            return

        if parsed.path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return

        if parsed.path == "/guest":
            self._send(200, GUEST or {})
            return

        if parsed.path == "/api":
            p = (qs.get("p") or [""])[0]
            try:
                code, obj = proxy(p)
            except RuntimeError as e:
                code, obj = 429, {"code": -429, "message": str(e)}
            except Exception as e:
                code, obj = 500, {"code": -500, "message": "本地代理异常：%s" % e}
            self._send(code, obj)
            return

        if parsed.path == "/playurl":
            bvid = (qs.get("bvid") or [""])[0]
            if not bvid or not bvid.isalnum():
                self._send(400, {"code": -1, "message": "bvid 不合法"})
                return
            qn = (qs.get("qn") or ["80"])[0]
            if not qn.isdigit():
                qn = "80"
            ep = (qs.get("ep") or [""])[0]
            try:
                st = resolve_stream(bvid, qn, ep)
            except Exception as e:
                self._send(502, {"code": -502, "message": str(e)})
                return
            self._send(200, {
                "code": 0,
                "bvid": bvid,
                "title": st["title"],
                "qn": st.get("qn"),
                "qualities": st.get("accept_quality") or [],
                "quality_names": st.get("accept_desc") or [],
                "quality": st["quality"],
                "format": st["format"],
                "length": st["length"],
                "src": "/stream?bvid=" + bvid + (("&ep=" + ep) if ep else ""),
                "danmaku": "/danmaku?bvid=" + bvid + (("&ep=" + ep) if ep else ""),
            })
            return

        if parsed.path == "/stream":
            bvid = (qs.get("bvid") or [""])[0]
            qn = (qs.get("qn") or ["80"])[0]
            ep = (qs.get("ep") or [""])[0]
            if not qn.isdigit():
                qn = "80"
            if bvid and bvid.isalnum():
                self._serve_stream(bvid, qn, ep)
            else:
                self._send(400, {"code": -1, "message": "bvid 不合法"})
            return

        if parsed.path == "/danmaku":
            bvid = (qs.get("bvid") or [""])[0]
            ep = (qs.get("ep") or [""])[0]
            if bvid and bvid.isalnum():
                self._serve_danmaku(bvid, ep)
            else:
                self._send(400, {"code": -1, "message": "bvid 不合法"})
            return

        if parsed.path == "/fav/folders":
            dt = cookie_dict()
            mid = dt.get("DedeUserID", "")
            if not mid:
                self._send(200, {"code": -101, "message": "需要登录后才能读取收藏夹"})
                return
            SESS.warm()
            st, body = http_get("https://api.bilibili.com/x/v3/fav/folder/created/list-all?up_mid=%s" % mid)
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "收藏夹读取失败（HTTP %s）" % st})
            return

        if parsed.path == "/fav/items":
            media_id = (qs.get("media_id") or [""])[0]
            pn = (qs.get("page") or ["1"])[0]
            if not media_id.isdigit():
                self._send(400, {"code": -1, "message": "media_id 不合法"})
                return
            st, body = http_get("https://api.bilibili.com/x/v3/fav/resource/list"
                                "?media_id=%s&pn=%s&ps=20&platform=web" % (media_id, pn))
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "收藏内容读取失败（HTTP %s）" % st})
            return

        if parsed.path == "/history":
            pn = (qs.get("page") or ["1"])[0]
            dt = cookie_dict()
            if not dt.get("DedeUserID"):
                self._send(200, {"code": -101, "message": "需要登录后才能读取观看历史"})
                return
            st, body = http_get("https://api.bilibili.com/x/v2/history?pn=%s&ps=20" % pn,
                                referer="https://www.bilibili.com/account/history")
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "历史读取失败（HTTP %s）" % st})
            return

        if parsed.path == "/me/detail":
            st, body = http_get("https://api.bilibili.com/x/web-interface/nav")
            try:
                d = json.loads(body)
            except Exception:
                self._send(502, {"code": -502, "message": "账号信息读取失败"})
                return
            data = d.get("data") or {}
            if data.get("isLogin"):
                mid = data.get("mid")
                extra = {}
                st2, body2 = http_get("https://api.bilibili.com/x/relation/stat?vmid=%s" % mid)
                try:
                    extra = json.loads(body2).get("data") or {}
                except Exception:
                    pass
                data["relation"] = extra
            self._send(200, d)
            return

        if parsed.path == "/upload/system":
            try:
                webbrowser.open("https://member.bilibili.com/platform/upload/video/frame")
                self._send(200, {"code": 0, "message": "已在系统浏览器打开官方投稿页"})
            except Exception as e:
                self._send(200, {"code": -1, "message": "打开失败：%s" % e})
            return

        if parsed.path == "/upload/open":
            launch_window("https://member.bilibili.com/platform/upload/video/frame",
                          os.path.join(os.path.expanduser("~"), ".msz-bili-pc", "window"))
            self._send(200, {"code": 0, "message": "已打开官方投稿窗口"})
            return

        if parsed.path == "/drafts":
            self._send(200, {"code": 0, "data": load_state().get("drafts", [])})
            return

        if parsed.path == "/draft/save":
            body = self._read_body()
            st = load_state()
            d = st.get("drafts") or []
            d.insert(0, {"title": body.get("title", ""), "desc": body.get("desc", ""),
                         "tag": body.get("tag", ""), "tid": body.get("tid", ""),
                         "file": body.get("file", ""), "at": time.strftime("%Y-%m-%d %H:%M")})
            st["drafts"] = d[:50]
            save_state(st)
            self._send(200, {"code": 0, "message": "草稿已保存"})
            return

        if parsed.path == "/upload/tids":
            self._send(200, {"code": 0, "data": [{"name": n, "tid": t} for n, t in REGION_TIDS]})
            return

        if parsed.path == "/upload/status":
            jid = (qs.get("id") or [""])[0]
            st = UP_STATE.get(jid)
            if not st:
                self._send(200, {"code": -1, "message": "没有这个任务"})
            else:
                self._send(200, {"code": 0, "data": st})
            return

        if parsed.path == "/live/list":
            page = (qs.get("page") or ["1"])[0]
            if not page.isdigit():
                page = "1"
            st, body = http_get("https://api.live.bilibili.com/room/v1/room/get_user_recommend"
                                "?page=%s&page_size=30" % page, referer="https://live.bilibili.com/")
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "直播列表读取失败（HTTP %s）" % st})
            return

        if parsed.path == "/live/room":
            rid = (qs.get("room_id") or [""])[0]
            if not rid.isdigit():
                self._send(400, {"code": -1, "message": "room_id 不合法"})
                return
            url = ("https://api.live.bilibili.com/xlive/web-room/v2/index/getRoomPlayInfo"
                   "?room_id=%s&protocol=0,1&format=0,1,2&codec=0,1&qn=1000&platform=web&ptype=8" % rid)
            st, body = http_get(url, referer="https://live.bilibili.com/%s" % rid)
            try:
                d = json.loads(body)
            except Exception:
                self._send(502, {"code": -502, "message": "直播信息读取失败（HTTP %s）" % st})
                return
            if d.get("code") != 0:
                self._send(200, d)
                return
            data = d.get("data") or {}
            m3u8 = ""
            try:
                for stm in data["playurl_info"]["playurl"]["stream"]:
                    if stm.get("protocol_name") != "http_hls":
                        continue
                    fm = stm["format"][0]
                    cd = fm["codec"][0]
                    ui = cd["url_info"][0]
                    m3u8 = ui["host"] + cd["base_url"] + ui["extra"]
                    break
            except Exception:
                pass
            self._send(200, {"code": 0, "data": {
                "room_id": data.get("room_id"),
                "live": data.get("live_status"),
                "m3u8": m3u8,
                "title": (data.get("room_info") or {}).get("title", ""),
            }})
            return

        if parsed.path == "/lm3u8":
            u = (qs.get("u") or [""])[0]
            if not u.startswith("http"):
                self._send(400, {"code": -1, "message": "参数不合法"})
                return
            st, body = http_get(u, referer="https://live.bilibili.com/")
            raw = body.decode("utf-8", "replace") if isinstance(body, bytes) else str(body)
            if not raw.startswith("#EXTM3U"):
                self._send(502, {"code": -502, "message": "不是有效的 m3u8"})
                return
            base = u.split("?")[0]
            out = []
            for line in raw.splitlines():
                t = line.strip()
                if not t:
                    continue
                if t.startswith("#"):
                    out.append(t)
                    continue
                absu = t if t.startswith("http") else (
                    base[:base.rfind("/") + 1] + t if "/" in t else base[:base.rfind("/") + 1] + t)
                out.append("/lseg?u=" + urllib.parse.quote(absu, safe=""))
            body2 = ("\n".join(out) + "\n").encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl")
            self.send_header("Content-Length", str(len(body2)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body2)
            return

        if parsed.path == "/lseg":
            u = (qs.get("u") or [""])[0]
            if not u.startswith("http"):
                self._send(400, {"code": -1, "message": "参数不合法"})
                return
            try:
                req = urllib.request.Request(u, headers={
                    "User-Agent": UA,
                    "Referer": "https://live.bilibili.com/",
                    "Accept-Encoding": "identity",
                })
                with SESS.opener.open(req, timeout=20) as r:
                    data = r.read()
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
            except Exception:
                self.send_response(502)
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        if parsed.path == "/bangumi":
            page = (qs.get("page") or ["1"])[0]
            st = (qs.get("st") or ["1"])[0]
            if not page.isdigit():
                page = "1"
            url = ("https://api.bilibili.com/pgc/season/index/result?season_type=1&st=%s"
                   "&sort=0&page=%s&pagesize=20&type=1" % (st, page))
            st2, body = http_get(url, referer="https://www.bilibili.com/bangumi/")
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "番剧列表读取失败（HTTP %s）" % st2})
            return

        if parsed.path == "/bangumi/season":
            sid = (qs.get("season_id") or [""])[0]
            if not sid.isdigit():
                self._send(400, {"code": -1, "message": "season_id 不合法"})
                return
            url = "https://api.bilibili.com/pgc/view/web/season?season_id=%s" % sid
            st2, body = http_get(url, referer="https://www.bilibili.com/bangumi/")
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "番剧详情读取失败（HTTP %s）" % st2})
            return

        if parsed.path == "/download":
            bvid = (qs.get("bvid") or [""])[0]
            qn = (qs.get("qn") or ["64"])[0]
            title = (qs.get("title") or [bvid])[0]
            if not bvid:
                self._send(400, {"code": -1, "message": "缺少 bvid"})
                return
            try:
                job = start_download(bvid, qn, title)
                self._send(200, {"code": 0, "message": "已开始下载（约 %s）" % job["name"]})
            except Exception as e:
                self._send(502, {"code": -502, "message": "下载启动失败：%s" % e})
            return

        if parsed.path == "/downloads":
            self._send(200, {"code": 0, "data": {"dir": DOWNLOAD_DIR,
                                                "list": list_downloads()}})
            return

        if parsed.path == "/region":
            rid = (qs.get("rid") or ["0"])[0]
            if not rid.isdigit():
                rid = "0"
            st, body = http_get("https://api.bilibili.com/x/web-interface/ranking/v2?rid=%s&type=all" % rid)
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "分区排行读取失败（HTTP %s）" % st})
            return

        if parsed.path == "/space/info":
            mid = (qs.get("mid") or [""])[0]
            if not mid.isdigit():
                self._send(400, {"code": -1, "message": "mid 不合法"})
                return
            try:
                code, obj = api_wbi("/x/space/wbi/acc/info",
                                    dict({"mid": mid, "platform": "web"}, **SPACE_EXTRA))
                if code == 200 and isinstance(obj, dict) and obj.get("code") == 0:
                    dd = obj.get("data") or {}
                    st, body = http_get("https://api.bilibili.com/x/relation/stat?vmid=%s" % mid)
                    try:
                        dd["follower"] = (json.loads(body).get("data") or {}).get("follower", 0)
                    except Exception:
                        pass
                    time.sleep(random.uniform(0.4, 0.9))
                    st2, body2 = http_get("https://api.bilibili.com/x/space/navnum?mid=%s" % mid)
                    try:
                        dd["archive_count"] = (json.loads(body2).get("data") or {}).get("video", 0)
                    except Exception:
                        pass
                    time.sleep(random.uniform(0.4, 0.9))
                    st3, body3 = http_get("https://api.bilibili.com/x/space/upstat?mid=%s" % mid)
                    try:
                        dd["like_num"] = (json.loads(body3).get("data") or {}).get("likes", 0)
                    except Exception:
                        pass
                    obj["data"] = dd
            except RuntimeError as e:
                code, obj = 429, {"code": -429, "message": str(e)}
            except Exception as e:
                code, obj = 500, {"code": -500, "message": "签名请求异常：%s" % e}
            self._send(code, obj)
            return

        if parsed.path == "/space/videos":
            mid = (qs.get("mid") or [""])[0]
            pn = (qs.get("page") or ["1"])[0]
            name = (qs.get("name") or [""])[0]
            if not mid.isdigit():
                self._send(400, {"code": -1, "message": "mid 不合法"})
                return
            if not pn.isdigit():
                pn = "1"
            obj = None
            for attempt in (1, 2):
                try:
                    code, obj = api_wbi("/x/space/wbi/arc/search", dict({
                        "mid": mid, "ps": "24", "pn": pn, "order": "pubdate",
                        "platform": "web", "web_location": "1550101"}, **SPACE_EXTRA))
                except RuntimeError as e:
                    code, obj = 429, {"code": -429, "message": str(e)}
                    break
                except Exception as e:
                    code, obj = 500, {"code": -500, "message": "签名请求异常：%s" % e}
                    break
                if isinstance(obj, dict) and obj.get("code") == 0:
                    break
                if isinstance(obj, dict) and obj.get("code") in (-412, -352) and attempt == 1:
                    time.sleep(2.0)
                    continue
                break

            # 空间接口被风控时，退回到按 UP 名搜索并过滤作者
            if (not isinstance(obj, dict)) or obj.get("code") != 0:
                if name:
                    GUARD.wait_turn()
                    SESS.warm()
                    url = ("https://api.bilibili.com/x/web-interface/search/type"
                           "?search_type=video&keyword=%s&page=%s&page_size=24"
                           % (urllib.parse.quote(name), pn))
                    st, body = http_get(url, referer="https://search.bilibili.com/")
                    try:
                        sobj = json.loads(body)
                    except Exception:
                        sobj = {"code": -1}
                    items = ((sobj.get("data") or {}).get("result") or [])
                    vlist = []
                    for it in items:
                        author = re.sub(r"<[^>]+>", "", str(it.get("author") or ""))
                        if author.strip() != name.strip():
                            continue
                        vlist.append({
                            "bvid": it.get("bvid"), "title": it.get("title"),
                            "pic": it.get("pic"), "play": it.get("play"),
                            "duration": it.get("duration"), "author": author})
                    self._send(200, {"code": 0, "from": "search",
                                     "data": {"list": {"vlist": vlist},
                                              "page": {"count": 100000}}})
                    return
                self._send(200, obj if isinstance(obj, dict) else {"code": -1})
                return
            self._send(200, obj)
            return

        if parsed.path == "/article":
            cid = (qs.get("id") or [""])[0]
            if not cid.isdigit():
                self._send(400, {"code": -1, "message": "专栏 id 不合法"})
                return
            SESS.warm()
            status, body = http_get("https://api.bilibili.com/x/article/view?id=%s" % cid,
                                    referer="https://www.bilibili.com/read/cv%s" % cid)
            try:
                self._send(200, json.loads(body))
            except Exception:
                self._send(502, {"code": -502, "message": "专栏读取失败（HTTP %s）" % status})
            return

        if parsed.path == "/login/qr":
            status, body = http_get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate?source=main-fe-header",
                referer="https://www.bilibili.com/")
            try:
                d = json.loads(body)
                data = d.get("data") or {}
                self._send(200, {"code": d.get("code", -1), "url": data.get("url", ""),
                                 "key": data.get("qrcode_key", "")})
            except Exception as e:
                self._send(502, {"code": -502, "message": "二维码生成失败：%s" % e})
            return

        if parsed.path == "/login/poll":
            key = (qs.get("key") or [""])[0]
            if not key:
                self._send(400, {"code": -1, "message": "缺少 key"})
                return
            status, body = http_get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/poll?source=main-fe-header&qrcode_key="
                + urllib.parse.quote(key), referer="https://www.bilibili.com/")
            try:
                d = json.loads(body)
            except Exception:
                self._send(502, {"code": -502, "message": "轮询失败"})
                return
            data = d.get("data") or {}
            code = data.get("code", d.get("code"))
            if code is None:
                code = d.get("code")
            if code == 0:
                save_cookies()
                try:
                    push_cookies_to_browser()
                except Exception:
                    pass
                _, nav = http_get("https://api.bilibili.com/x/web-interface/nav")
                try:
                    info = json.loads(nav).get("data") or {}
                except Exception:
                    info = {}
                self._send(200, {"code": 0, "state": "done", "name": info.get("uname", ""),
                                 "level": info.get("level_info", {}).get("current_level", 0),
                                 "vip": bool((info.get("vipStatus") or 0) > 0)})
                return
            # 86101 未扫码 / 86090 已扫码未确认 / 86038 二维码已失效
            state = {86101: "waiting", 86090: "scanned", 86038: "expired"}.get(code, "waiting")
            self._send(200, {"code": code, "state": state})
            return

        if parsed.path == "/me":
            try_import_browser_login()
            _, body = http_get("https://api.bilibili.com/x/web-interface/nav")
            try:
                d = json.loads(body)
                data = d.get("data") or {}
                self._send(200, {"isLogin": bool(data.get("isLogin")),
                                 "name": data.get("uname", ""),
                                 "mid": data.get("mid", 0),
                                 "level": (data.get("level_info") or {}).get("current_level", 0),
                                 "vip": bool((data.get("vipStatus") or 0) > 0),
                                 "face": data.get("face", "")})
            except Exception as e:
                self._send(200, {"isLogin": False, "message": str(e)})
            return

        if parsed.path == "/login/sync":
            mode = (qs.get("mode") or [""])[0]
            try:
                if mode == "to-browser":
                    n = push_cookies_to_browser()
                    self._send(200, {"code": 0, "synced": n, "dir": "to-browser", "message": "已把客户端登录态写入浏览器"})
                else:
                    n = pull_cookies_from_browser()
                    self._send(200, {"code": 0, "synced": n, "dir": "from-browser", "message": "已把浏览器登录态取回客户端"})
                return
            except Exception as e:
                self._send(200, {"code": -1, "message": str(e), "synced": 0})
                return

        if parsed.path == "/logout":
            SESS.jar.clear()
            st = load_state()
            st.pop("cookies", None)
            save_state(st)
            self._send(200, {"code": 0, "message": "已退出登录"})
            return

        if parsed.path == "/quit":
            self._send(200, {"code": 0, "message": "程序已退出"})
            threading.Thread(target=_shutdown_all, daemon=True).start()
            return

        if parsed.path == "/open":
            u = (qs.get("u") or [""])[0]
            if u.startswith("https://") and "bilibili.com" in u:
                threading.Thread(target=open_external, args=(u,), daemon=True).start()
                self._send(200, {"ok": True})
            else:
                self._send(400, {"code": -1, "message": "链接不合法"})
            return

        self._send(404, {"code": -1, "message": "not found"})


def _shutdown_all():
    time.sleep(0.4)
    try:
        srv_ref[0].shutdown()
    except Exception:
        pass
    time.sleep(0.2)
    os._exit(0)


srv_ref = [None]


def open_external(url):
    try:
        if platform.system() == "Windows":
            os.startfile(url)
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", url])
        else:
            subprocess.Popen(["xdg-open", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)


# ----------------------------------------------------------------------------
# 浏览器应用窗口
# ----------------------------------------------------------------------------
def find_browser():
    sysname = platform.system()
    if sysname == "Windows":
        roots = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 os.environ.get("LOCALAPPDATA", "")]
        rels = [r"Microsoft\Edge\Application\msedge.exe",
                r"Google\Chrome\Application\chrome.exe",
                r"BraveSoftware\Brave-Browser\Application\brave.exe"]
        for r in roots:
            if not r:
                continue
            for rel in rels:
                p = os.path.join(r, rel)
                if os.path.exists(p):
                    return p
    else:
        for name in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser", "brave-browser"):
            for d in os.environ.get("PATH", "").split(os.pathsep):
                p = os.path.join(d, name)
                if os.path.exists(p) and os.access(p, os.X_OK):
                    return p
    return None


CDP_PORT = 0


def launch_window(url, profile_dir):
    global CDP_PORT
    exe = find_browser()
    if not exe:
        webbrowser.open(url)
        return None
    try:
        CDP_PORT = free_port(9600)
    except Exception:
        CDP_PORT = 0
    args = [exe, "--app=" + url, "--window-size=1280,860", "--user-data-dir=" + profile_dir,
            "--no-first-run", "--no-default-browser-check",
            "--remote-debugging-port=" + str(CDP_PORT), "--remote-allow-origins=*",
            "--disable-features=Translate,BlockThirdPartyCookies,ThirdPartyStoragePartitioning"]
    try:
        return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        webbrowser.open(url)
        return None


# ---------------- 浏览器 Cookie 同步（CDP，标准库实现） ----------------
def _ws_connect(ws_url):
    u = urllib.parse.urlparse(ws_url)
    host, port = u.hostname, u.port or 80
    path = u.path + (("?" + u.query) if u.query else "")
    sk = socket.create_connection((host, port), timeout=4)
    key = base64.b64encode(os.urandom(16)).decode()
    req = ("GET %s HTTP/1.1\r\nHost: %s:%d\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n" % (path, host, port, key))
    sk.sendall(req.encode())
    buf = b""
    while b"\r\n\r\n" not in buf:
        try:
            d = sk.recv(4096)
        except socket.timeout:
            raise RuntimeError("CDP 握手超时")
        if not d:
            raise RuntimeError("CDP 握手断开")
        buf += d
    head, rest = buf.split(b"\r\n\r\n", 1)
    if b"101" not in head.split(b"\r\n")[0]:
        raise RuntimeError("CDP 拒绝连接")
    return sk, rest


def _ws_send(sk, obj):
    data = json.dumps(obj).encode("utf-8")
    mask = os.urandom(4)
    n = len(data)
    hdr = bytearray([0x81])
    if n < 126:
        hdr.append(0x80 | n)
    elif n < 65536:
        hdr.append(0x80 | 126)
        hdr += struct.pack(">H", n)
    else:
        hdr.append(0x80 | 127)
        hdr += struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    sk.sendall(bytes(hdr) + mask + masked)


def _ws_recv(sk, buf):
    while len(buf) < 2:
        buf += sk.recv(65536)
    b1, b2 = buf[0], buf[1]
    ln = b2 & 0x7F
    pos = 2
    if ln == 126:
        while len(buf) < pos + 2:
            buf += sk.recv(65536)
        ln = struct.unpack(">H", buf[pos:pos + 2])[0]
        pos += 2
    elif ln == 127:
        while len(buf) < pos + 8:
            buf += sk.recv(65536)
        ln = struct.unpack(">Q", buf[pos:pos + 8])[0]
        pos += 8
    while len(buf) < pos + ln:
        buf += sk.recv(65536)
    payload = buf[pos:pos + ln]
    return (b1 & 0x0F), payload, buf[pos + ln:]


def cdp_call(method, params=None, pick="page"):
    """向当前浏览器窗口发一条 CDP 命令，返回 result。"""
    if not CDP_PORT:
        raise RuntimeError("调试端口未启用（请从启动器重新打开窗口）")
    try:
        raw = urllib.request.urlopen("http://127.0.0.1:%d/json/list" % CDP_PORT, timeout=4).read()
        targets = json.loads(raw)
    except Exception as e:
        raise RuntimeError("读不到浏览器调试端口：%s" % e)
    cand = [t for t in targets if t.get("type") == "page"]
    if not cand:
        raise RuntimeError("没有可用的浏览器页面")
    prefer = [t for t in cand if "member.bilibili.com" in (t.get("url") or "")]
    tgt = (prefer or cand)[0]
    sk, buf = _ws_connect(tgt["webSocketDebuggerUrl"])
    try:
        _ws_send(sk, {"id": 1, "method": method, "params": params or {}})
        while True:
            op, payload, buf = _ws_recv(sk, buf)
            if op == 0x8:
                raise RuntimeError("CDP 连接被关闭")
            if op == 0x9:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            if msg.get("id") == 1:
                if msg.get("error"):
                    raise RuntimeError(str(msg["error"]))
                return msg.get("result") or {}
    finally:
        try:
            sk.close()
        except Exception:
            pass


def browser_cookies():
    res = cdp_call("Storage.getCookies")
    out = []
    for c in res.get("cookies") or []:
        if "bilibili.com" in (c.get("domain") or ""):
            out.append(c)
    return out


def push_cookies_to_browser():
    """把客户端登录态灌进浏览器（让内嵌投稿页自动已登录）。"""
    cookies = cookie_dict()
    if not cookies:
        return 0
    n = 0
    for name, value in cookies.items():
        try:
            cdp_call("Network.setCookie", {"name": name, "value": value,
                                           "domain": ".bilibili.com", "path": "/",
                                           "secure": True, "httpOnly": name in
                                           ("SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5", "sid")})
            n += 1
        except Exception:
            pass
    return n


def pull_cookies_from_browser():
    """把浏览器里（投稿页）的登录态拉回客户端。"""
    got = browser_cookies()
    n = 0
    for c in got:
        try:
            ck = http.cookiejar.Cookie(
                0, c["name"], c["value"], None, False, c.get("domain") or ".bilibili.com",
                True, False, c.get("path") or "/", True, bool(c.get("secure")),
                int(c["expires"]) if c.get("expires") else None, False, None, None, {})
            SESS.jar.set_cookie(ck)
            n += 1
        except Exception:
            pass
    if n:
        save_cookies()
    return n


def free_port(start=8765):
    for p in range(start, start + 60):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            s.close()
            return p
        except OSError:
            s.close()
    return start


def main():
    global GUEST
    print("%s v%s" % (APP_NAME, APP_VERSION))
    load_cookies()
    print("正在初始化游客身份…")
    GUEST = ensure_guest()
    print("游客身份：%s" % GUEST.get("name"))

    port = free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv_ref[0] = srv
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    url = "http://127.0.0.1:%d/" % port
    print("本地服务：%s" % url)

    if os.environ.get("MSZ_NO_WINDOW") == "1":
        print("调试模式：不打开窗口，按 Ctrl+C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        srv.shutdown()
        return

    proc = launch_window(url, os.path.join(STATE_DIR, "window"))
    if proc is not None:
        print("已打开应用窗口，关闭窗口即退出。")
        try:
            proc.wait()
        except KeyboardInterrupt:
            pass
    else:
        print("未找到 Chromium 系浏览器，已用默认浏览器打开；按 Ctrl+C 退出。")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    srv.shutdown()
    print("已退出。")


if __name__ == "__main__":
    main()
