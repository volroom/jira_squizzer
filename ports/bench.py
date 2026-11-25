#!/usr/bin/env python3
"""Benchmark the Go original against the Rust, Python and TypeScript ports.

Usage: python3 ports/bench.py [--ts5 PATH_TO_TSC5] [--runs N]
Writes ports/results.json and prints a Markdown report.
Stdlib only. CPU and peak RSS come from os.wait4 (the process tree's waited-for children are included).
"""

import argparse
import json
import os
import pty
import re
import shutil
import statistics
import struct
import subprocess
import fcntl
import tempfile
import termios
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
P = ROOT / "ports"
GO, RS, PY, TS = ROOT, P / "rust", P / "python", P / "typescript"
TMP = Path(tempfile.mkdtemp(prefix="squizz-bench-"))


# ---------- measuring ----------


def run(cmd, cwd, env=None):
    """Run cmd; return (wall s, cpu s, peak RSS MB)."""
    err = tempfile.TemporaryFile()
    t = time.perf_counter()
    p = subprocess.Popen(cmd, cwd=cwd, env={**os.environ, **(env or {})}, stdout=subprocess.DEVNULL, stderr=err, shell=isinstance(cmd, str))
    _, status, ru = os.wait4(p.pid, 0)
    wall = time.perf_counter() - t
    if os.waitstatus_to_exitcode(status) != 0:
        err.seek(0)
        raise RuntimeError(f"{cmd} failed:\n{err.read().decode()[-2000:]}")
    return wall, ru.ru_utime + ru.ru_stime, ru.ru_maxrss / 1024


def bench(cmd, cwd, n, env=None, before=None):
    """Median of n runs. `before` runs untimed ahead of each run (e.g. to clear a cache)."""
    rs = []
    for _ in range(n):
        if before:
            before()
        rs.append(run(cmd, cwd, env))
    return {k: round(statistics.median(r[i] for r in rs), 3) for i, k in enumerate(("wall_s", "cpu_s", "peak_rss_mb"))}


class edited:
    """Append a comment to a source file for the duration of the block (forces an incremental rebuild)."""

    def __init__(self, path, comment):
        self.path, self.comment = Path(path), comment

    def __enter__(self):
        self.orig = self.path.read_bytes()
        return self

    def bump(self):
        self.path.write_bytes(self.orig + f"\n{self.comment} bench {time.time_ns()}\n".encode())

    def __exit__(self, *a):
        self.path.write_bytes(self.orig)


def incremental(cmd, cwd, n, path, comment, env=None):
    with edited(path, comment) as e:
        return bench(cmd, cwd, n, env, before=e.bump)


def fresh(path):
    def f():
        shutil.rmtree(path, ignore_errors=True)
        Path(path).mkdir(parents=True)

    return f


def du(path):
    return round(sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file() and not f.is_symlink()) / 1e6, 2)


# ---------- size ----------

COMMENT = {".go": "//", ".rs": "//", ".ts": "//", ".tsx": "//", ".py": "#"}


def loc(files):
    """Non-blank, non-comment lines (Python docstrings count as code)."""
    code = chars = 0
    for f in files:
        mark, block = COMMENT[f.suffix], False
        for line in f.read_text().splitlines():
            s = line.strip()
            chars += len(s)
            if mark == "//" and (block or s.startswith("/*")):
                block = "*/" not in s
                continue
            if s and not s.startswith(mark):
                code += 1
    return {"files": len(files), "code_lines": code, "chars": chars}


def sizes():
    return {
        "go": {"src": loc([GO / "jira.go", GO / "main.go"]), "test": loc([GO / "main_test.go"])},
        "rust": {"src": loc([RS / "src/main.rs", RS / "src/jira.rs"]), "test": loc([RS / "src/tests.rs"])},
        "python": {"src": loc(sorted((PY / "src").rglob("*.py"))), "test": loc(sorted((PY / "tests").rglob("*.py")))},
        "typescript": {
            "src": loc([f for f in sorted((TS / "src").glob("*.ts*")) if ".test." not in f.name]),
            "test": loc(sorted((TS / "src").glob("*.test.ts"))),
        },
    }


def deps():
    out = lambda c, d: subprocess.run(c, cwd=d, capture_output=True, text=True, check=True).stdout  # noqa: E731
    count = lambda f, pat: len(re.findall(pat, (f).read_text(), re.M))  # noqa: E731
    lock = json.loads((TS / "package-lock.json").read_text())["packages"]
    pkg = json.loads((TS / "package.json").read_text())
    return {
        "go": {"direct": len(re.findall(r"^\t\S+ v\S+$", (GO / "go.mod").read_text(), re.M)), "total": len(set(out(["go", "list", "-deps", "-f", "{{if .Module}}{{.Module.Path}}{{end}}", "."], GO).split())) - 1},
        "rust": {"direct": 4, "total": len(json.loads(out(["cargo", "metadata", "-q", "--format-version", "1", "--filter-platform", "x86_64-unknown-linux-gnu"], RS))["resolve"]["nodes"]) - 1},
        "python": {"direct": 2, "total": count(PY / "uv.lock", r"^\[\[package\]\]") - 1},
        "typescript": {
            "direct": len(pkg["dependencies"]) + len(pkg["devDependencies"]),
            "total": len([k for k in lock if k]),
            "total_runtime": len([k for k, v in lock.items() if k and not v.get("dev")]),
        },
    }


# ---------- builds ----------


def builds(n, ts5):
    cold_n = max(2, n // 2)
    r = {}
    gocache = TMP / "gocache"
    r["go"] = {
        "cold": bench(["go", "build", "-o", TMP / "go-bin", "."], GO, cold_n, {"GOCACHE": str(gocache)}, fresh(gocache)),
        "incremental": incremental(["go", "build", "-o", TMP / "go-bin", "."], GO, n, GO / "main.go", "//"),
        "noop": bench(["go", "build", "-o", TMP / "go-bin", "."], GO, n),
    }
    tgt = TMP / "cargo-target"
    for prof, flag in (("debug", []), ("release", ["--release"])):
        r[f"rust_{prof}"] = {
            "cold": bench(["cargo", "build", "-q", *flag], RS, cold_n, {"CARGO_TARGET_DIR": str(tgt)}, fresh(tgt)),
            "incremental": incremental(["cargo", "build", "-q", *flag], RS, n, RS / "src/main.rs", "//"),
            "noop": bench(["cargo", "build", "-q", *flag], RS, n),
        }
    venv = TMP / "venv"
    r["python"] = {
        "cold": bench(["uv", "sync", "-q", "--frozen"], PY, cold_n, {"UV_PROJECT_ENVIRONMENT": str(venv)}, fresh(venv)),
        "noop": bench(["uv", "sync", "-q", "--frozen"], PY, n),
        "bytecompile": bench(["python3", "-m", "compileall", "-q", "-f", "src"], PY, n),
    }
    out = TMP / "tsout"
    for name, tsc in (("typescript_tsc7", TS / "node_modules/.bin/tsc"), ("typescript_tsc5", Path(ts5))):
        r[name] = {"full": bench([tsc, "-p", ".", "--outDir", out], TS, n, before=fresh(out))}
    # Dependency install from a warm package cache (cold project dir).
    tsdir = TMP / "ts-install"

    def tscopy():
        fresh(tsdir)()
        for f in ("package.json", "package-lock.json"):
            shutil.copy(TS / f, tsdir)

    r["typescript_npm_ci"] = {"cold": bench("npm ci -s --prefer-offline --no-audit --no-fund", tsdir, cold_n, before=tscopy)}
    return r


# ---------- tests ----------


def tests(n, ts5):
    out = "dist-ts5"  # inside the project so node can resolve node_modules
    tsc5 = f"rm -rf {out} && {ts5} -p . --outDir {out} && node --test '{out}/*.test.js'"
    return {
        "go": {
            "run": bench(["go", "test", "-count=1", "./..."], GO, n),
            "edit_and_test": incremental(["go", "test", "-count=1", "./..."], GO, n, GO / "main.go", "//"),
        },
        "rust": {
            "run": bench(["cargo", "test", "-q"], RS, n),
            "edit_and_test": incremental(["cargo", "test", "-q"], RS, n, RS / "src/main.rs", "//"),
        },
        "python": {
            "run": bench([PY / ".venv/bin/python", "-m", "pytest", "-q", "-p", "no:cacheprovider"], PY, n),
            "uv_run": bench(["uv", "run", "-q", "pytest", "-q", "-p", "no:cacheprovider"], PY, n),
        },
        "typescript": {
            "run": bench("node --test 'dist/*.test.js'", TS, n),
            "build_and_test_tsc7": bench("npm test --silent", TS, n),
            "build_and_test_tsc5": bench(tsc5, TS, n),
        },
    } | (shutil.rmtree(TS / out) or {})


# ---------- runtime (fake Jira + pty) ----------

STATUSES = [("1", "To Do"), ("2", "In Progress"), ("3", "Code Review"), ("4", "Done")]
ISSUES = [
    {
        "id": str(10000 + i),
        "key": f"FAKE-{i}",
        "fields": {
            "summary": f"Ticket number {i} with a reasonably long summary to wrap",
            "status": {"id": STATUSES[i % 4][0], "name": STATUSES[i % 4][1]},
            "assignee": {"displayName": "Ada Lovelace"} if i % 3 else None,
            "issuetype": {"name": "Story"},
        },
    }
    for i in range(1, 41)
]
ROUTES = {
    "/rest/agile/1.0/board": {"values": [{"id": 1, "type": "scrum"}]},
    "/rest/agile/1.0/board/1/configuration": {"columnConfig": {"columns": [{"name": "Backlog", "statuses": []}] + [{"name": n, "statuses": [{"id": i}]} for i, n in STATUSES]}},
    "/rest/agile/1.0/board/1/sprint": {"values": [{"id": 7, "name": "Sprint 7"}]},
    "/rest/agile/1.0/sprint/7/issue": {"issues": ISSUES, "total": len(ISSUES)},
}


class Jira(BaseHTTPRequestHandler):
    def do_GET(self):
        body = ROUTES.get(self.path.split("?")[0])
        if body is None:
            self.send_error(404)
            return
        b = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


def proc_cpu(pid):
    f = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(f[11]) + int(f[12])) / os.sysconf("SC_CLK_TCK")


def proc_mem(pid):
    s = Path(f"/proc/{pid}/status").read_text()
    get = lambda k: int(re.search(rf"^{k}:\s+(\d+)", s, re.M).group(1)) / 1024  # noqa: E731
    return get("VmRSS"), get("VmHWM")


TERM_REPLIES = {
    b"\x1b]11;?": b"\x1b]11;rgb:2828/2828/2828\x1b\\",  # background colour
    b"\x1b]10;?": b"\x1b]10;rgb:ebeb/dbdb/b2b2\x1b\\",  # foreground colour
    b"\x1b[6n": b"\x1b[1;1R",  # cursor position
    b"\x1b[c": b"\x1b[?62;22c",  # primary device attributes
}


def session(cmd, env):
    """Start the app in a 140x40 pty, wait for the board, idle, send keys, quit."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvpe(cmd[0], cmd, env)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 140, 0, 0))
    t0 = time.perf_counter()
    buf, seen = bytearray(), threading.Event()

    def reader():
        while True:
            try:
                d = os.read(fd, 65536)
            except OSError:
                return
            if not d:
                return
            buf.extend(d)
            # Answer terminal queries like a real terminal would (termenv waits up to 5s otherwise).
            for q, a in TERM_REPLIES.items():
                if q in d:
                    os.write(fd, a)
            if b"FAKE-1" in buf:
                seen.set()

    threading.Thread(target=reader, daemon=True).start()
    if not seen.wait(30):
        os.kill(pid, 9)
        raise RuntimeError(f"{cmd}: board never appeared:\n{bytes(buf)[-500:]!r}")
    ready = time.perf_counter() - t0
    time.sleep(1)
    c0 = proc_cpu(pid)
    time.sleep(3)
    idle_cpu = (proc_cpu(pid) - c0) / 3
    rss, _ = proc_mem(pid)
    c0, t1 = proc_cpu(pid), time.perf_counter()
    for k in "jjjjllllkkkkhhhh" * 10:  # 160 navigation keys
        os.write(fd, k.encode())
        time.sleep(0.005)
    time.sleep(0.5)
    nav_cpu = proc_cpu(pid) - c0
    _, hwm = proc_mem(pid)
    t2 = time.perf_counter()
    os.write(fd, b"q")
    _, status, _ = os.wait4(pid, 0)
    quit_s = time.perf_counter() - t2
    os.close(fd)
    return {"startup_s": ready, "idle_cpu_pct": idle_cpu * 100, "idle_rss_mb": rss, "peak_rss_mb": hwm, "nav_160_keys_cpu_s": nav_cpu, "quit_s": quit_s}


def runtime(n):
    subprocess.run(["go", "build", "-o", TMP / "go-bin", "."], cwd=GO, check=True)
    subprocess.run(["cargo", "build", "-q", "--release"], cwd=RS, check=True)
    subprocess.run(["npm", "run", "-s", "build"], cwd=TS, check=True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Jira)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    cfg = TMP / "xdg"
    cfg.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "XDG_CONFIG_HOME": str(cfg),
        "JIRA_BASE_URL": f"http://127.0.0.1:{srv.server_port}",
        "JIRA_USERNAME": "bench@example.com",
        "JIRA_PROJECT": "FAKE",
        "JIRA_API_KEY": "x",
        "TERM": "xterm-256color",
        "COLORTERM": "truecolor",
    }
    apps = {
        "go": [str(TMP / "go-bin")],
        "rust": [str(RS / "target/release/jira-squizzer")],
        "python": [str(PY / ".venv/bin/jira-squizzer")],
        "typescript": ["node", str(TS / "dist/app.js")],
    }
    r = {}
    for name, cmd in apps.items():
        rs = [session(cmd, env) for _ in range(n)]
        r[name] = {k: round(statistics.median(x[k] for x in rs), 3) for k in rs[0]}
    srv.shutdown()
    r["_artifact_mb"] = {
        "go_binary": round((TMP / "go-bin").stat().st_size / 1e6, 2),
        "rust_release_binary": round((RS / "target/release/jira-squizzer").stat().st_size / 1e6, 2),
        "python_venv": du(PY / ".venv"),
        "typescript_node_modules_all": du(TS / "node_modules"),
        "typescript_dist": du(TS / "dist"),
    }
    prod = TMP / "ts-prod"
    fresh(prod)()
    for f in ("package.json", "package-lock.json"):
        shutil.copy(TS / f, prod)
    subprocess.run("npm ci -s --omit=dev --prefer-offline --no-audit --no-fund", shell=True, cwd=prod, check=True)
    r["_artifact_mb"]["typescript_node_modules_runtime_only"] = du(prod / "node_modules")
    nod = TMP / "py-nodev"
    subprocess.run(["uv", "sync", "-q", "--frozen", "--no-dev"], cwd=PY, env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(nod)}, check=True)
    r["_artifact_mb"]["python_venv_runtime_only"] = du(nod)
    return r


def versions(ts5):
    v = lambda c, d=ROOT: subprocess.run(c, cwd=d, capture_output=True, text=True, shell=True).stdout.strip()  # noqa: E731
    return {
        "go": v("go version"),
        "rustc": v("rustc --version"),
        "python": v(".venv/bin/python --version", PY),
        "uv": v("uv --version"),
        "node": v("node --version"),
        "tsc7": v("./node_modules/.bin/tsc -v", TS),
        "tsc5": v(f"{ts5} -v"),
        "cpus": os.cpu_count(),
        "kernel": os.uname().release,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ts5", required=True, help="path to a TypeScript 5 tsc")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--sections", default="size,deps,build,test,runtime", help="comma-separated; others are kept from results.json")
    a = ap.parse_args()
    out = P / "results.json"
    res = json.loads(out.read_text()) if out.exists() else {}
    res |= {"versions": versions(a.ts5), "runs": a.runs}
    for name, fn in (("size", sizes), ("deps", deps), ("build", lambda: builds(a.runs, a.ts5)), ("test", lambda: tests(a.runs, a.ts5)), ("runtime", lambda: runtime(a.runs))):
        if name not in a.sections.split(","):
            continue
        print(f"== {name}", flush=True)
        res[name] = fn()
        out.write_text(json.dumps(res, indent=1, default=str))
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    main()
