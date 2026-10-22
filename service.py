"""聚合音乐站的服务守护进程 —— 拉起三个音乐 API + server.py，然后看住它们。

被 nssm 当 Windows 服务跑时（Session 0，没有控制台），bat 里那些
`start` 开新窗口、`timeout /t`、`tasklist /fi windowtitle` 全都不靠谱，
所以这里用 Python 重写：subprocess.Popen 直接持句柄，日志重定向到文件，
任何子进程挂了自动重启，父进程收到停止信号时把整棵进程树带走。

手动跑也行：python service.py   （Ctrl+C 退出并清理子进程）
"""

import json
import os
import signal
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(HERE, "logs")
NODE = r"C:\Program Files\nodejs\node.exe"
PY = sys.executable

# 名字 -> (工作目录候选, 启动命令, 端口)
def _backends():
    kugou = os.path.join(HERE, "vendor", "kugou-api")
    if not os.path.exists(os.path.join(kugou, "app.js")):
        kugou = os.path.join(HERE, "..", "danmuji", "vendor", "kugou-api")
    return [
        ("kugou",   kugou,                                     ["app.js"],        4000),
        ("netease", r"D:\Desktop\网\api-enhanced-4.40.0",       ["app.js"],        3001),
        ("qq",      r"D:\Desktop\网\QQMusicapi-main",           ["src/server.js"], 3301),
    ]


def log(msg):
    print("[agg %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# 被 nssm 当服务跑时 stdout 是管道，Python 默认全缓冲会让日志迟到很久
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


def port_up(port, timeout=0.2):
    """端口上有没有人在听。用 socket 直连，比解析 netstat 输出可靠。"""
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


def start_one(name, cwd, argv, port, logfile):
    """起一个子进程，日志写文件。返回 Popen 或 None（目录/入口不存在）。"""
    entry = os.path.join(cwd, *argv)
    if not os.path.exists(entry):
        log("%s: 找不到 %s，跳过" % (name, entry))
        return None
    env = dict(os.environ)
    env["PORT"] = str(port)
    fh = open(logfile, "ab", buffering=0)
    try:
        p = subprocess.Popen(
            [NODE] + argv, cwd=cwd, env=env,
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
        )
    except Exception as e:
        log("%s: 启动失败 %s" % (name, e))
        fh.close()
        return None
    log("%s: pid=%d port=%d" % (name, p.pid, port))
    return p


def kill_tree(p):
    if p is None or p.poll() is not None:
        return
    try:
        subprocess.run(["taskkill", "/f", "/t", "/pid", str(p.pid)],
                       capture_output=True, timeout=10,
                       creationflags=0x08000000)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def main():
    os.makedirs(LOG_DIR, exist_ok=True)
    procs = {}          # name -> (Popen, port, logfile path)
    stopping = []

    def shutdown(*_):
        if stopping:
            return
        stopping.append(1)
        log("收到停止信号，清理子进程…")
        for name, (p, _port, _lf) in list(procs.items()):
            kill_tree(p)
        log("已全部停止")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    try:
        signal.signal(signal.SIGTERM, shutdown)
        signal.signal(signal.SIGBREAK, shutdown)
    except (AttributeError, ValueError):
        pass   # 非 Windows 或缺信号时忽略

    # ---- 1. 三个音乐源 ----
    for name, cwd, argv, port in _backends():
        if port_up(port):
            log("%s: 端口 %d 已有人在听，跳过" % (name, port))
            continue
        lf = os.path.join(LOG_DIR, name + ".log")
        p = start_one(name, cwd, argv, port, lf)
        if p:
            procs[name] = (p, port, lf)

    # 等后端把端口挂起来
    for name, cwd, argv, port in _backends():
        for _ in range(40):
            if port_up(port):
                log("%s: 端口 %d 就绪" % (name, port))
                break
            time.sleep(0.5)
        else:
            log("%s: 端口 %d 等不到（30 秒），先不管，继续" % (name, port))

    # ---- 2. 聚合站本体（前台跑，父进程挂了它也就没了）----
    srv_log = os.path.join(LOG_DIR, "server.log")
    env = dict(os.environ)
    fh = open(srv_log, "ab", buffering=0)
    srv = subprocess.Popen(
        [PY, os.path.join(HERE, "server.py")], cwd=HERE, env=env,
        stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
        creationflags=0x08000000,
    )
    log("server.py: pid=%d 端口 8808" % srv.pid)

    # ---- 3. 看住：谁挂了就重启谁，自己一直不死 ----
    backends = {n: (c, a, pt) for n, c, a, pt in _backends()}
    while not stopping:
        time.sleep(5)

        # 后端
        for name, (cwd, argv, port) in backends.items():
            cur = procs.get(name)
            if cur is None:
                if not port_up(port):
                    log("%s: 之前没起来，重试" % name)
                    lf = os.path.join(LOG_DIR, name + ".log")
                    p = start_one(name, cwd, argv, port, lf)
                    if p:
                        procs[name] = (p, port, lf)
                continue
            p, port, lf = cur
            if p.poll() is not None:
                log("%s: 退出了(code=%s)，5 秒后重启" % (name, p.returncode))
                time.sleep(5)
                procs[name] = (start_one(name, cwd, argv, port, lf), port, lf)
                if procs[name][0] is None:
                    procs.pop(name, None)

        # 本体
        if srv.poll() is not None:
            log("server.py: 退出了(code=%s)，5 秒后重启" % srv.returncode)
            time.sleep(5)
            fh = open(srv_log, "ab", buffering=0)
            srv = subprocess.Popen(
                [PY, os.path.join(HERE, "server.py")], cwd=HERE, env=env,
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                creationflags=0x08000000,
            )
            log("server.py: 重启 pid=%d" % srv.pid)


if __name__ == "__main__":
    main()
