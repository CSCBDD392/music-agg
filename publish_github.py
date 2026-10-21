"""把 danmuji 发布到 GitHub（只用 GitHub REST API，不需要 git/gh）。

凭据查找顺序：
  1. 环境变量 GITHUB_TOKEN / GH_TOKEN
  2. 参数 --token
  3. 本文件同目录的 .github-token（一行纯文本，建议加进 .gitignore）

用法：
  python publish_github.py --user 你的用户名 --repo danmuji --dry-run   # 先看会传哪些文件
  python publish_github.py --user 你的用户名 --repo danmuji            # 真传
  python publish_github.py --user 你的用户名 --repo danmuji --private  # 传私有库
"""
import argparse
import base64
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.github.com"
# 不上传的东西
SKIP_NAMES = {".git", "__pycache__", ".github-token", "config.local.json",
              "node_modules", ".pkgcache"}
SKIP_EXT = {".pyc", ".png", ".jpg", ".log", ".zip"}
DESC = "B 站弹幕点歌机 + 正方形弹幕面板：弹幕点歌 → 酷狗网页版播放；弹幕实时投影到任意窗口/屏幕旁"


def token_of(explicit=""):
    for v in (explicit, os.environ.get("GITHUB_TOKEN"), os.environ.get("GH_TOKEN")):
        if v:
            return v.strip()
    p = os.path.join(HERE, ".github-token")
    if os.path.exists(p):
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    return ""


def req(method, path, token, data=None, raw=None):
    url = path if path.startswith("http") else API + path
    body = raw if raw is not None else (json.dumps(data).encode() if data is not None else None)
    r = urllib.request.Request(url, data=body, method=method)
    r.add_header("Authorization", "token " + token)
    r.add_header("Accept", "application/vnd.github+json")
    r.add_header("User-Agent", "danmuji-publisher")
    if body is not None:
        r.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except Exception:
            return e.code, {}


def files_to_sync(root):
    out = []
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in SKIP_NAMES and not x.startswith("_dbg")]
        for n in names:
            if n in SKIP_NAMES or n.startswith("_") and n != "_gen_page.py":
                continue
            if os.path.splitext(n)[1].lower() in SKIP_EXT:
                continue
            p = os.path.join(d, n)
            rel = os.path.relpath(p, root).replace("\\", "/")
            if rel.startswith("."):
                continue
            with open(p, "rb") as f:
                out.append((rel, f.read()))
    out.sort()
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True, help="GitHub 用户名")
    ap.add_argument("--repo", default="danmuji")
    ap.add_argument("--token", default="")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default="danmuji: 弹幕点歌机 + 弹幕面板")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    files = files_to_sync(HERE)
    total = sum(len(b) for _, b in files)
    print("待上传 %d 个文件，共 %.1f KB" % (len(files), total / 1024))
    for rel, b in files:
        print("  %-24s %6d B" % (rel, len(b)))
    if a.dry_run:
        return

    token = token_of(a.token)
    if not token:
        sys.exit("没找到 token：设 GITHUB_TOKEN 环境变量，或写进 .github-token")

    st, me = req("GET", "/user", token)
    if st != 200:
        sys.exit("token 无效（HTTP %s: %s）" % (st, me.get("message")))
    print("已登录：%s" % me.get("login"))

    st, r = req("GET", "/repos/%s/%s" % (a.user, a.repo), token)
    if st == 404:
        st2, r = req("POST", "/user/repos", token,
                     {"name": a.repo, "description": DESC, "private": bool(a.private),
                      "has_issues": True, "has_wiki": False})
        if st2 not in (200, 201):
            sys.exit("建仓库失败（HTTP %s: %s）" % (st2, r.get("message")))
        print("已新建仓库 %s" % r["html_url"])
    else:
        print("仓库已存在：%s" % r.get("html_url"))

    st, r = req("GET", "/repos/%s/%s/git/ref/heads/%s" % (a.user, a.repo, a.branch), token)
    if st == 200:
        ref = r["object"]["sha"]
        print("提交到已有分支 %s（%s）" % (a.branch, ref[:8]))
    else:
        # 空仓库：Git Data API 会 409，用 Contents API 建出默认分支
        ref = None
        r0 = files[0][0] or "README.md"
        st2, r2 = req("PUT", "/repos/%s/%s/contents/%s" % (a.user, a.repo, r0), token,
                      {"message": "init", "content": base64.b64encode(b"# init\n").decode(),
                       "branch": a.branch})
        if st2 not in (200, 201):
            sys.exit("初始化仓库失败（HTTP %s: %s）" % (st2, r2.get("message")))
        print("已初始化空仓库")

    # 已有文件逐个 PUT（带 sha 就是覆盖），新文件直接建
    existing = {}
    if ref:
        st, tree = req("GET", "/repos/%s/%s/git/trees/%s?recursive=1" % (a.user, a.repo, ref),
                       token)
        if st == 200:
            existing = {t["path"]: t["sha"] for t in tree.get("tree", [])
                        if t["type"] == "blob"}

    ok, changed = 0, 0
    for rel, data in files:
        body = {"message": a.message + ": " + rel,
                "content": base64.b64encode(data).decode(), "branch": a.branch}
        # 先查这个文件在分支上是否已存在，已存在必须带 sha 才能覆盖
        st, cur = req("GET", "/repos/%s/%s/contents/%s?ref=%s"
                      % (a.user, a.repo, rel, a.branch), token)
        if isinstance(cur, dict) and cur.get("sha"):
            if _same(cur, data):
                ok += 1
                continue
            body["sha"] = cur["sha"]
        elif st not in (404,):
            log = cur.get("message") if isinstance(cur, dict) else cur
            print("  跳过 %s（探测失败 HTTP %s: %s）" % (rel, st, log))
            continue
        st, r = req("PUT", "/repos/%s/%s/contents/%s" % (a.user, a.repo, rel), token, body)
        if st not in (200, 201):
            print("  FAIL %s 失败（HTTP %s: %s）" % (rel, st, r.get("message")))
            continue
        ok += 1
        changed += 1
        print("  OK %s" % rel)
    print("完成：%d 个文件（更新 %d）→ https://github.com/%s/%s"
          % (ok, changed, a.user, a.repo))


def _same(resp, data):
    """内容相同就不重复提交（比较 git blob sha）。"""
    import hashlib
    h = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
    return resp.get("sha") == h


if __name__ == "__main__":
    main()

