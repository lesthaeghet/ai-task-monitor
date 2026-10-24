#!/usr/bin/env python3
"""taskmon - run a command, show its full live output in a tabbed GUI window, and
hand the calling agent only a compact summary.  Stdlib only (tkinter for the GUI).

    taskmon run -n build -- npm run build
    taskmon summary build | tail build -n 40 | grep build "error" | list
    taskmon install-skill        # teach Claude Code to use it

State lives in $TASKMON_HOME (default ~/.taskmon)/tasks/<name>/:
    meta.json    status, exit code, timing, paths
    output.log   full output (ANSI stripped)
    display.log  human-readable rendering (agent JSON streams); for plain text
                 commands display == output.log
    summary.txt  the compact summary (refreshed every few seconds while running)
    partial.txt  the current unterminated line (progress bars), shown live in the GUI
    stop         flag file; its presence makes the runner kill the task
"""
import argparse
import codecs
import collections
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

__version__ = "0.1.0"

HOME =os.path.abspath(os.path.expanduser(os.environ.get("TASKMON_HOME", "~/.taskmon")))
TASKS = os.path.join(HOME, "tasks")
IS_WIN = os.name == "nt"
CREATE_NEW_PROCESS_GROUP = 0x00000200
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
ERR_RE = re.compile(r"\b(errors?|fail(?:ed|ure|ures|s)?|fatal|exception|traceback|panic(?:ked)?"
                    r"|segmentation fault|undefined reference|cannot find|not found)\b", re.I)
NOT_ERR_RE = re.compile(r"\b(?:0|no) (?:errors?|fail(?:ed|ures?)?)\b|\berrors?: 0\b", re.I)
WARN_RE = re.compile(r"\bwarn(?:ing)?s?\b", re.I)
NOT_WARN_RE = re.compile(r"\b(?:0|no) warnings?\b", re.I)
ICONS = {"running": "▶", "ok": "✔", "failed": "✘", "stopped": "■",
         "timeout": "⌛", "lost": "?"}


# ---------------------------------------------------------------- helpers

def task_dir(name):
    return os.path.join(TASKS, name)


def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write_file(path, text):
    """Write via temp+replace; retry because Windows refuses to replace a file
    another process (the GUI) has open for a moment."""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for _ in range(20):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.05)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.remove(tmp)


def pid_alive(pid):
    if not pid:
        return False
    if IS_WIN:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return bool(ok) and code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_tree(pid):
    if IS_WIN:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except OSError:
            pass


def spawn_detached(argv, env=None, window=False):
    """Start a process that outlives us (and the agent's shell / job object)."""
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, close_fds=True, env=env)
    if not IS_WIN:
        return subprocess.Popen(argv, start_new_session=True, **kw)
    flags = CREATE_NEW_PROCESS_GROUP | (DETACHED_PROCESS if window else CREATE_NO_WINDOW)
    try:
        return subprocess.Popen(argv, creationflags=flags | CREATE_BREAKAWAY_FROM_JOB, **kw)
    except OSError:  # job does not allow breakaway
        return subprocess.Popen(argv, creationflags=flags, **kw)


def effective_status(meta):
    st = meta.get("status", "?")
    if st == "running" and not pid_alive(meta.get("pid")):
        return "lost"
    return st


def fmt_dur(sec):
    sec = max(0.0, sec or 0.0)
    if sec < 60:
        return f"{sec:.1f}s"
    if sec < 3600:
        return f"{int(sec // 60)}m{int(sec % 60):02d}s"
    return f"{int(sec // 3600)}h{int(sec % 3600 // 60):02d}m"


def elapsed(meta):
    end = meta.get("end") or time.time()
    return end - meta.get("start", end)


def clip(s, width):
    return s if len(s) <= width else f"{s[:width]} ...[+{len(s) - width} chars]"


def oneline(s, width):
    return clip(" ".join(str(s).split()), width)


def sanitize(name):
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.")
    return name[:60] or "task"


def load_meta_or_die(name):
    meta = read_json(os.path.join(task_dir(name), "meta.json"))
    if not meta:
        known = ", ".join(sorted(os.listdir(TASKS))) if os.path.isdir(TASKS) else ""
        sys.exit(f"[taskmon] no task named '{name}'" + (f" (known: {known})" if known else ""))
    return meta


def is_err(line):
    return bool(ERR_RE.search(line)) and not NOT_ERR_RE.search(line)


def is_warn(line):
    return bool(WARN_RE.search(line)) and not NOT_WARN_RE.search(line)


# ------------------------------------------------- agent stream renderers

def tool_brief(inp, width=160):
    if not isinstance(inp, dict):
        return oneline(inp, width)
    for k in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt"):
        if inp.get(k):
            return oneline(inp[k], width)
    return oneline(json.dumps(inp, ensure_ascii=False), width)


def flatten(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(flatten(c.get("text", "") if isinstance(c, dict) else c) for c in content)
    return "" if content is None else str(content)


def text_lines(prefix, text):
    lines = str(text).strip("\n").splitlines() or [""]
    return [prefix + lines[0]] + ["  " + ln for ln in lines[1:]]


class AgentState:
    def __init__(self):
        self.result = None
        self.last_text = None
        self.tools = collections.Counter()
        self.tool_errors = 0
        self.turns = None
        self.cost = None
        self.is_error = False
        self.extra = []  # short "k=v" facts for the summary header


def render_claude(obj, st):
    """`claude -p --output-format stream-json --verbose` events -> display lines."""
    t = obj.get("type")
    out = []
    if t == "system":
        sub = str(obj.get("subtype"))
        if sub == "init":
            out.append(f"[init] model={obj.get('model')} cwd={obj.get('cwd')}")
        elif sub == "compact_boundary" or re.search(r"error|retry|fail", sub):
            out.append(f"[system] {sub}")
        # other subtypes (thinking_tokens, post_turn_summary, ...) are progress chatter
    elif t == "assistant":
        for c in (obj.get("message") or {}).get("content") or []:
            ct = c.get("type")
            if ct == "text" and c.get("text", "").strip():
                st.last_text = c["text"]
                out += text_lines("assistant: ", c["text"])
            elif ct == "thinking":
                out.append("[thinking] " + oneline(c.get("thinking", ""), 160))
            elif ct == "tool_use":
                st.tools[c.get("name", "?")] += 1
                out.append(f"-> {c.get('name')}: {tool_brief(c.get('input'))}")
    elif t == "user":
        content = (obj.get("message") or {}).get("content")
        for c in content if isinstance(content, list) else []:
            if isinstance(c, dict) and c.get("type") == "tool_result":
                txt = flatten(c.get("content")).strip()
                n = txt.count("\n") + 1 if txt else 0
                if c.get("is_error"):
                    st.tool_errors += 1
                    out.append("   <- ERROR: " + oneline(txt, 200))
                else:
                    out.append("   <- " + oneline(txt, 120) + (f"  (+{n - 1} lines)" if n > 1 else ""))
    elif t == "result":
        st.result = obj.get("result")
        st.turns = obj.get("num_turns")
        st.cost = obj.get("total_cost_usd")
        st.is_error = bool(obj.get("is_error")) or obj.get("subtype") not in (None, "success")
        out.append(f"[result] {obj.get('subtype')} turns={st.turns} cost=${st.cost}")
        if st.result and st.result != st.last_text:
            out += text_lines("", st.result)
    elif t == "stream_event" or str(t).endswith("_event"):
        pass
    else:
        out.append(oneline(json.dumps(obj, ensure_ascii=False), 200))
    return out


def render_codex(obj, st):
    """`codex exec --json` events -> display lines."""
    t = obj.get("type", "")
    item = obj.get("item") or {}
    it = item.get("type")
    out = []
    if t == "thread.started":
        out.append(f"[init] thread {obj.get('thread_id')}")
    elif t == "item.started" and it == "command_execution":
        out.append(f"-> exec: {oneline(item.get('command', ''), 160)}")
    elif t == "item.completed":
        if it == "agent_message":
            st.last_text = st.result = item.get("text", "")
            out += text_lines("assistant: ", st.result)
        elif it == "reasoning":
            out.append("[thinking] " + oneline(item.get("text", ""), 160))
        elif it == "command_execution":
            st.tools["exec"] += 1
            code = item.get("exit_code")
            if code not in (0, None):
                st.tool_errors += 1
            txt = (item.get("aggregated_output") or "").strip()
            tag = "ERROR " if code not in (0, None) else ""
            out.append(f"   <- {tag}exit {code}: {oneline(txt, 120)}")
        elif it == "file_change":
            st.tools["file_change"] += 1
            paths = [c.get("path", "?") for c in item.get("changes") or []]
            out.append("-> file_change: " + oneline(", ".join(paths), 160))
        elif it == "mcp_tool_call":
            st.tools[f"mcp:{item.get('tool')}"] += 1
            out.append(f"-> mcp {item.get('server')}.{item.get('tool')}")
        elif it == "web_search":
            st.tools["web_search"] += 1
            out.append(f"-> web_search: {oneline(item.get('query', ''), 160)}")
        elif it == "error":
            st.is_error = True
            out.append(f"[error] {oneline(item.get('message', ''), 300)}")
        elif it != "todo_list":
            out.append(f"[{it}] " + oneline(json.dumps(item, ensure_ascii=False), 160))
    elif t == "turn.completed":
        u = obj.get("usage") or {}
        st.turns = (st.turns or 0) + 1
        st.extra = [f"in={u.get('input_tokens')}", f"out={u.get('output_tokens')}"]
    elif t in ("turn.failed", "error"):
        st.is_error = True
        msg = (obj.get("error") or {}).get("message") if t == "turn.failed" else obj.get("message")
        out.append(f"[error] {oneline(msg or obj, 300)}")
    elif t not in ("turn.started", "item.started", "item.updated"):
        out.append(oneline(json.dumps(obj, ensure_ascii=False), 200))
    return out


RENDERERS = {"claude": render_claude, "codex": render_codex}


def detect_format(line):
    if not line.startswith("{"):
        return "text"
    try:
        obj = json.loads(line)
    except ValueError:
        return "text"
    if isinstance(obj, dict):
        if obj.get("type") == "system" and "session_id" in obj:
            return "claude"
        if str(obj.get("type", "")).startswith("thread."):
            return "codex"
    return "text"


# ---------------------------------------------------------------- runner

class Collector:
    """Receives output lines, writes the logs, and keeps just enough state to
    build the summary without re-reading anything."""

    def __init__(self, a, d, meta):
        self.a, self.d, self.meta = a, d, meta
        self.lock = threading.Lock()
        self.fmt = a.format
        self.raw_path = os.path.join(d, "output.log")
        self.disp_path = os.path.join(d, "display.log")
        self.raw = open(self.raw_path, "w", encoding="utf-8", newline="\n")
        self.disp = None
        self.lines = 0          # raw lines
        self.dlines = 0         # display lines
        self.tail = collections.deque(maxlen=max(1, a.tail))
        self.user_re = re.compile("|".join(f"(?:{g})" for g in a.grep), re.I) if a.grep else None
        self.matches = {}       # dedupe key -> [lineno, line, count]
        self.match_lines = 0
        self.warns = 0
        self.agent = AgentState()
        self.partial = ""       # current unterminated line, set by pump()
        if self.fmt != "auto":
            self._set_format(self.fmt)

    def _set_format(self, fmt):
        self.fmt = fmt
        if fmt == "text":
            self.meta["display"] = self.raw_path
        else:
            self.disp = open(self.disp_path, "w", encoding="utf-8", newline="\n")
            self.meta["display"] = self.disp_path
        self.meta["format"] = fmt

    def feed(self, line):
        line = CTRL_RE.sub("", ANSI_RE.sub("", line))
        with self.lock:
            self.lines += 1
            self.raw.write(line + "\n")
            if self.fmt == "auto":
                if not line.strip():
                    return
                self._set_format(detect_format(line))
            if self.fmt == "text":
                self._display(line, scan=True)
                return
            try:
                obj = json.loads(line) if line.startswith("{") else None
            except ValueError:
                obj = None
            if not isinstance(obj, dict):  # stderr chatter from the CLI itself
                self._display(line, scan=True)
                return
            for dl in RENDERERS[self.fmt](obj, self.agent):
                self._display(dl, scan=False)

    def _display(self, line, scan):
        self.dlines += 1
        if self.disp:
            self.disp.write(line + "\n")
        self.tail.append((self.dlines, line))
        hit = self.user_re is not None and self.user_re.search(line)
        if scan and not self.a.no_default_grep:
            if is_err(line):
                hit = True
            elif is_warn(line):
                self.warns += 1
        if hit:
            self.match_lines += 1
            key = line.strip()
            if key in self.matches:
                self.matches[key][2] += 1
            elif len(self.matches) < self.a.max_matches:
                self.matches[key] = [self.dlines, line, 1]

    def flush(self):
        with self.lock:
            self.raw.flush()
            if self.disp:
                self.disp.flush()

    def close(self):
        with self.lock:
            self.raw.close()
            if self.disp:
                self.disp.close()
            if self.fmt == "auto":  # never produced output
                self.meta["display"] = self.raw_path
                self.meta["format"] = "text"

    def summary(self):
        with self.lock:
            m, a, w = self.meta, self.a, self.a.width
            st = m["status"]
            head = f"[taskmon] {m['name']}: "
            if st == "running":
                head += f"RUNNING | {fmt_dur(elapsed(m))} so far"
            else:
                head += f"{st.upper()} (exit {m.get('exit_code')}) | {fmt_dur(elapsed(m))}"
            head += f" | {self.dlines} lines"
            out = [head]
            ag = self.agent
            agentic = self.fmt in RENDERERS
            if agentic:
                facts = []
                if ag.turns is not None:
                    facts.append(f"turns={ag.turns}")
                if ag.cost is not None:
                    facts.append(f"cost=${ag.cost:.4f}" if isinstance(ag.cost, (int, float)) else f"cost={ag.cost}")
                facts += ag.extra
                if ag.tools:
                    facts.append("tools: " + ", ".join(f"{k} x{v}" for k, v in ag.tools.most_common(8)))
                if ag.tool_errors:
                    facts.append(f"tool errors={ag.tool_errors}")
                if facts:
                    out.append(" | ".join(facts))
            out.append(f"log: {m.get('display') or self.raw_path}")
            first_tail = self.tail[0][0] if self.tail else self.dlines + 1
            shown = [v for v in self.matches.values() if v[0] < first_tail]
            if shown:
                extra = f", {len(self.matches)} unique of {self.match_lines}" if self.match_lines > len(shown) else ""
                out.append(f"-- matching lines before the tail ({len(shown)} shown{extra}) --")
                for n, line, cnt in shown:
                    out.append(f"{n}: {clip(line, w)}" + (f"  (x{cnt})" if cnt > 1 else ""))
            if self.warns:
                out.append(f"warnings: {self.warns} lines (see: grep {m['name']} warn)")
            final = ag.result if ag.result is not None else (ag.last_text if st != "running" else None)
            if agentic and final:
                out.append("-- result --" if ag.result is not None else "-- last assistant message --")
                out.append(clip(final.strip(), a.result_chars))
            if not agentic or st != "ok" or ag.is_error or not final:
                if self.tail:
                    label = "output" if first_tail == 1 else f"last {len(self.tail)} lines"
                    out.append(f"-- {label} --")
                    out += [clip(line, w) for _, line in self.tail]
            return "\n".join(out) + "\n"


def split_lines(buf):
    """Split decoded output into complete lines; a bare CR (progress bars)
    overwrites the line in progress.  Returns (lines, pending)."""
    hold = ""
    if buf.endswith("\r"):
        buf, hold = buf[:-1], "\r"
    parts = buf.replace("\r\n", "\n").split("\n")
    pending = parts.pop()
    if "\r" in pending:
        pending = pending[pending.rfind("\r") + 1:]
    lines = []
    for p in parts:
        if "\r" in p:
            segs = [s for s in p.split("\r") if s.strip()]
            p = segs[-1] if segs else ""
        lines.append(p)
    if len(pending) > 100_000:
        lines.append(pending)
        pending = ""
    return lines, pending + hold


def pump(stream, col):
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    pending = ""
    while True:
        chunk = stream.read1(65536)
        if not chunk:
            break
        lines, pending = split_lines(pending + dec.decode(chunk))
        for ln in lines:
            col.feed(ln)
        col.flush()
        # unterminated line (progress bar, prompt) - published live via partial.txt
        col.partial = CTRL_RE.sub("", ANSI_RE.sub("", pending.rstrip("\r")))[-500:]
    col.partial = ""
    rest = (pending + dec.decode(b"", final=True)).rstrip("\r")
    if rest:
        col.feed(rest)
    col.flush()


# One GUI per TASKMON_HOME.  It holds an OS lock on HOME/gui.lock for its whole
# life; the OS drops the lock if it dies, so there is no stale state and two
# homes (e.g. a test or demo home) never fight over a window.

def _lock(f):
    """Non-blocking exclusive lock on byte 0; raises OSError if already held."""
    if IS_WIN:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f):
    if IS_WIN:
        import msvcrt
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def gui_running():
    os.makedirs(HOME, exist_ok=True)
    with open(os.path.join(HOME, "gui.lock"), "a+") as f:
        try:
            _lock(f)
        except OSError:
            return True
        _unlock(f)
        return False


def launch_gui():
    exe = sys.executable
    if IS_WIN:
        pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
        exe = pyw if os.path.exists(pyw) else exe
    spawn_detached([exe, os.path.abspath(__file__), "gui"], window=True)


def ensure_gui(a):
    if a.no_gui or os.environ.get("TASKMON_NO_GUI") or gui_running():
        return
    launch_gui()


def cmd_run(a):
    argv = list(a.command)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv and not a.sh:
        sys.exit("[taskmon] nothing to run: use  run -n NAME -- command args  or  --sh \"cmd\"")
    display_cmd = a.sh or subprocess.list2cmdline(argv) if IS_WIN else a.sh or " ".join(argv)
    name = sanitize(os.environ.get("TASKMON_NAME") or a.name or
                    f"{os.path.splitext(os.path.basename(argv[0] if argv else a.sh.split()[0]))[0]}-{time.strftime('%H%M%S')}")
    d = task_dir(name)
    meta_path = os.path.join(d, "meta.json")
    old = read_json(meta_path)
    if old and effective_status(old) == "running" and old.get("pid") != os.getpid():
        sys.exit(f"[taskmon] task '{name}' is already running; pick another -n NAME or stop it")

    if a.detach and not os.environ.get("TASKMON_DETACHED"):
        run_id = uuid.uuid4().hex
        env = dict(os.environ, TASKMON_DETACHED="1", TASKMON_RUN_ID=run_id, TASKMON_NAME=name)
        spawn_detached([sys.executable, os.path.abspath(__file__)] + sys.argv[1:], env=env)
        for _ in range(100):
            m = read_json(meta_path)
            if m and m.get("run_id") == run_id:
                print(f"[taskmon] started '{name}' detached; use: wait {name}")
                return 0
            time.sleep(0.1)
        sys.exit(f"[taskmon] detached runner for '{name}' did not start")

    os.makedirs(d, exist_ok=True)
    for fn in ("stop", "summary.txt", "display.log", "output.log", "partial.txt"):
        p = os.path.join(d, fn)
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
        except OSError:
            open(p, "w").close()

    meta = {"name": name, "run_id": os.environ.get("TASKMON_RUN_ID") or uuid.uuid4().hex,
            "cmd": display_cmd, "cwd": os.path.abspath(a.cwd or os.getcwd()), "status": "running",
            "exit_code": None, "start": time.time(), "end": None, "pid": os.getpid(),
            "format": a.format, "display": None, "log": os.path.join(d, "output.log")}
    col = Collector(a, d, meta)
    sum_path = os.path.join(d, "summary.txt")

    def save():
        meta["lines"] = col.dlines
        write_file(meta_path, json.dumps(meta, indent=1))
        write_file(sum_path, col.summary())

    env = os.environ.copy()
    for k in ("TASKMON_DETACHED", "TASKMON_RUN_ID", "TASKMON_NAME"):
        env.pop(k, None)
    env.setdefault("PYTHONUNBUFFERED", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    kw = dict(cwd=a.cwd or None, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
              stdin=open(a.input, "rb") if a.input else subprocess.DEVNULL)
    if IS_WIN:
        kw["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True

    save()
    ensure_gui(a)
    try:
        if a.sh:
            proc = subprocess.Popen(a.sh, shell=True, **kw)
        else:
            proc = subprocess.Popen([shutil.which(argv[0]) or argv[0]] + argv[1:], **kw)
    except OSError as e:
        col.feed(f"[taskmon] could not start: {e}")
        col.close()
        meta.update(status="failed", exit_code=127, end=time.time())
        save()
        print(col.summary(), end="")
        return 127
    meta["child_pid"] = proc.pid
    save()

    reader = threading.Thread(target=pump, args=(proc.stdout, col), daemon=True)
    reader.start()
    forced = None
    last_save = time.time()
    stop_path = os.path.join(d, "stop")
    partial_path, last_partial = os.path.join(d, "partial.txt"), ""
    try:
        while True:
            try:
                rc = proc.wait(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                pass
            if forced is None and os.path.exists(stop_path):
                forced = "stopped"
                kill_tree(proc.pid)
            elif forced is None and a.timeout and time.time() - meta["start"] > a.timeout:
                forced = "timeout"
                kill_tree(proc.pid)
            if col.partial != last_partial:
                last_partial = col.partial
                write_file(partial_path, last_partial)
            if time.time() - last_save > 2:
                save()
                last_save = time.time()
    except KeyboardInterrupt:
        forced = "stopped"
        kill_tree(proc.pid)
        rc = proc.wait()
    reader.join(timeout=5)
    col.close()
    write_file(partial_path, "")
    if forced == "timeout":
        rc = 124
    meta.update(exit_code=rc, end=time.time(),
                status=forced or ("ok" if rc == 0 and not col.agent.is_error else "failed"))
    save()
    print(col.summary(), end="")
    return rc if rc else (0 if meta["status"] == "ok" else 1)


# ------------------------------------------------------- query commands

def read_summary(name):
    meta = load_meta_or_die(name)
    try:
        with open(os.path.join(task_dir(name), "summary.txt"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        text = f"[taskmon] {name}: {meta['status'].upper()} (no summary yet)\n"
    if effective_status(meta) == "lost":
        text = f"[taskmon] {name}: LOST (runner died without finishing)\n" + text
    return meta, text


def cmd_summary(a):
    for n in a.names:
        print(read_summary(n)[1], end="")


def display_path(meta):
    return meta.get("display") or meta.get("log")


def cmd_tail(a):
    meta = load_meta_or_die(a.name)
    path = meta["log"] if a.raw else display_path(meta)
    dq = collections.deque(maxlen=a.n)
    total = 0
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            total += 1
            dq.append((total, line.rstrip("\n")))
    print(f"[taskmon] {a.name}: {effective_status(meta)} | lines {total - len(dq) + 1}-{total} of {total}")
    for n, line in dq:
        print(f"{n}: {clip(line, a.width)}")


def cmd_grep(a):
    meta = load_meta_or_die(a.name)
    path = meta["log"] if a.raw else display_path(meta)
    rx = re.compile(a.pattern, 0 if a.case else re.I)
    with open(path, encoding="utf-8", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f]
    hits = [i for i, ln in enumerate(lines) if rx.search(ln)]
    print(f"[taskmon] {a.name}: {len(hits)} matching of {len(lines)} lines"
          + (f" (showing first {a.m})" if len(hits) > a.m else ""))
    last = -1
    for i in hits[:a.m]:
        lo, hi = max(i - a.C, last + 1), min(i + a.C, len(lines) - 1)
        if a.C and last >= 0 and lo > last + 1:
            print("--")
        for j in range(lo, hi + 1):
            print(f"{j + 1}{':' if j == i else '-'} {clip(lines[j], a.width)}")
        last = hi


def cmd_wait(a):
    for n in a.names:
        load_meta_or_die(n)
    deadline = time.time() + a.timeout if a.timeout else None
    remaining, worst = list(a.names), 0
    while remaining:
        for n in list(remaining):
            meta = read_json(os.path.join(task_dir(n), "meta.json")) or {}
            if effective_status(meta) != "running":
                print(read_summary(n)[1], end="")
                remaining.remove(n)
                worst = worst or (meta.get("exit_code") or (0 if meta.get("status") == "ok" else 1))
                if a.any:
                    return worst
        if not remaining:
            break
        if deadline and time.time() > deadline:
            for n in remaining:
                meta = read_json(os.path.join(task_dir(n), "meta.json")) or {}
                print(f"[taskmon] {n}: still RUNNING after {fmt_dur(elapsed(meta))}, "
                      f"{meta.get('lines', 0)} lines (wait again, or: summary {n})")
            return 124
        time.sleep(0.5)
    return worst


def all_metas():
    if not os.path.isdir(TASKS):
        return []
    metas = [read_json(os.path.join(TASKS, n, "meta.json")) for n in os.listdir(TASKS)]
    return sorted((m for m in metas if m), key=lambda m: m.get("start", 0))


def cmd_list(a):
    metas = all_metas()
    if not metas:
        print("[taskmon] no tasks")
    for m in metas:
        st = effective_status(m)
        code = f" exit={m.get('exit_code')}" if st not in ("running", "lost") else ""
        ago = "" if st == "running" else f"  ended {fmt_dur(time.time() - (m.get('end') or m['start']))} ago"
        print(f"{m['name']:<24} {st.upper()}{code:<9} {fmt_dur(elapsed(m)):>8}  {m.get('lines', 0)} lines{ago}")


def cmd_stop(a):
    for n in a.names:
        meta = load_meta_or_die(n)
        if effective_status(meta) != "running":
            print(f"[taskmon] {n} is not running")
            continue
        open(os.path.join(task_dir(n), "stop"), "w").close()
        print(f"[taskmon] stop requested for {n}")


def remove_finished(names=None):
    removed = []
    for m in all_metas():
        if (names is None or m["name"] in names) and effective_status(m) != "running":
            shutil.rmtree(task_dir(m["name"]), ignore_errors=True)
            removed.append(m["name"])
    return removed


def cmd_clean(a):
    removed = remove_finished(a.names or None)
    print(f"[taskmon] removed {len(removed)} finished task(s)" + (": " + ", ".join(removed) if removed else ""))


# ------------------------------------------------------------------- GUI

def cmd_gui(a):
    try:
        import tkinter as tk
        from tkinter import ttk
    except ImportError:
        sys.exit("[taskmon] tkinter is not available; install it (e.g. apt install python3-tk) "
                 "or run tasks with --no-gui")

    quit_flag = os.path.join(HOME, "gui.quit")
    if a.restart:
        # ask the running GUI to close itself (tasks are unaffected), then start fresh
        if gui_running():
            open(quit_flag, "w").close()
            for _ in range(50):
                time.sleep(0.2)
                if not gui_running():
                    break
            else:
                sys.exit("[taskmon] the running GUI did not close (it may predate --restart); "
                         "close its window and run: taskmon gui")
        launch_gui()
        print(f"[taskmon] GUI restarted for {TASKS}")
        return 0

    os.makedirs(TASKS, exist_ok=True)
    lock_file = open(os.path.join(HOME, "gui.lock"), "a+")  # held until this process exits
    try:
        _lock(lock_file)
    except OSError:
        print(f"[taskmon] GUI already running for {TASKS}")
        return 0
    try:
        os.remove(quit_flag)
    except OSError:
        pass

    BG, FG, PANEL = "#1e1e1e", "#d4d4d4", "#2b2b2b"
    MAX_LINES = 50_000

    def classify(line):
        if line.startswith("-> "):
            return "tool"
        if line.startswith("   <- ERROR") or line.startswith("[error]"):
            return "err"
        if line.startswith(("   <- ", "[thinking]", "[init]", "[system]")):
            return "dim"
        if line.startswith("assistant: "):
            return "assistant"
        if line.startswith("[result]"):
            return "result"
        if is_err(line):
            return "err"
        if is_warn(line):
            return "warn"
        return None

    class Tab:
        def __init__(self, app, meta):
            self.app, self.name = app, meta["name"]
            self.frame = ttk.Frame(app.nb)
            head = ttk.Frame(self.frame, style="Panel.TFrame", padding=(8, 4))
            head.pack(fill="x")
            self.status = ttk.Label(head, style="Status.TLabel")
            self.status.pack(side="left")
            for label, cmd in (("Close", self.close), ("Open log", self.open_log),
                               ("Agent view", self.agent_view), ("Stop", self.stop)):
                b = ttk.Button(head, text=label, command=cmd)
                b.pack(side="right", padx=2)
                if label == "Stop":
                    self.stop_btn = b
            self.info = ttk.Label(self.frame, style="Info.TLabel", padding=(8, 2))
            self.info.pack(fill="x")
            self.live = ttk.Label(self.frame, style="Live.TLabel", padding=(8, 3))
            self.live.pack(side="bottom", fill="x")
            body = ttk.Frame(self.frame)
            body.pack(fill="both", expand=True)
            self.text = tk.Text(body, bg=BG, fg=FG, insertbackground=FG, font=("Consolas", 10),
                                wrap="word", borderwidth=0, padx=6, pady=4, state="disabled")
            sb = ttk.Scrollbar(body, command=self.text.yview)
            self.text.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            self.text.pack(side="left", fill="both", expand=True)
            for tag, opts in {"err": {"foreground": "#f48771"}, "warn": {"foreground": "#d7ba7d"},
                              "tool": {"foreground": "#569cd6"}, "dim": {"foreground": "#7f7f7f"},
                              "assistant": {"foreground": "#b5cea8"},
                              "result": {"foreground": "#4ec9b0", "font": ("Consolas", 10, "bold")}}.items():
                self.text.tag_configure(tag, **opts)
            app.nb.add(self.frame, text=self.name)
            self.title = None
            self.reset(meta)

        def reset(self, meta):
            self.meta, self.run_id, self.path = meta, meta["run_id"], None
            self.offset, self.partial = 0, ""
            self.dec = codecs.getincrementaldecoder("utf-8")("replace")
            self.text.configure(state="normal")
            self.text.delete("1.0", "end")
            self.text.configure(state="disabled")

        def refresh(self, meta):
            self.meta = meta
            path = meta.get("display")
            if path != self.path:
                self.path, self.offset, self.partial = path, 0, ""
            if path:
                self.read_new()
            st = effective_status(meta)
            title = f"{ICONS.get(st, '?')} {self.name}"
            if title != self.title:
                self.app.nb.tab(self.frame, text=title)
                self.title = title
            code = "" if st in ("running", "lost") else f"  exit {meta.get('exit_code')}"
            self.status.configure(text=f"{st.upper()}{code}   {fmt_dur(elapsed(meta))}   "
                                       f"{meta.get('lines', 0)} lines   [{meta.get('format')}]")
            self.info.configure(text=f"$ {clip(meta.get('cmd', ''), 300)}      (cwd {meta.get('cwd')})")
            self.stop_btn.state(["!disabled"] if st == "running" else ["disabled"])
            live = ""
            if st == "running":
                try:
                    with open(os.path.join(task_dir(self.name), "partial.txt"), encoding="utf-8") as f:
                        live = f.read().strip()
                except OSError:
                    pass
            self.live.configure(text=f"⏵ {live}" if live else "")

        def read_new(self):
            try:
                size = os.path.getsize(self.path)
                if size < self.offset:
                    return
                if size == self.offset:
                    return
                with open(self.path, "rb") as f:
                    f.seek(self.offset)
                    data = f.read(min(size - self.offset, 512 * 1024))
            except OSError:
                return
            first = self.offset == 0
            self.offset += len(data)
            chunk = self.partial + self.dec.decode(data)
            lines = chunk.split("\n")
            self.partial = lines.pop()
            if not lines:
                return
            at_bottom = first or self.text.yview()[1] >= 0.999
            self.text.configure(state="normal")
            for line in lines:
                self.text.insert("end", line + "\n", classify(line) or ())
            count = int(self.text.index("end-1c").split(".")[0])
            if count > MAX_LINES:
                self.text.delete("1.0", f"{count - MAX_LINES}.0")
            self.text.configure(state="disabled")
            if at_bottom:
                self.text.see("end")

        def stop(self):
            open(os.path.join(task_dir(self.name), "stop"), "w").close()

        def open_log(self):
            path = self.meta.get("log")
            if path and os.path.exists(path):
                if IS_WIN:
                    os.startfile(path)
                else:
                    subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", path])

        def agent_view(self):
            win = tk.Toplevel(self.app.root)
            win.title(f"{self.name} - what the agent sees")
            win.geometry("900x500")
            t = tk.Text(win, bg=BG, fg=FG, font=("Consolas", 10), wrap="word", padx=6, pady=4)

            def load():
                t.delete("1.0", "end")
                try:
                    with open(os.path.join(task_dir(self.name), "summary.txt"), encoding="utf-8") as f:
                        t.insert("1.0", f.read())
                except OSError:
                    t.insert("1.0", "(no summary yet)")
            ttk.Button(win, text="Refresh", command=load).pack(anchor="e", padx=4, pady=2)
            t.pack(fill="both", expand=True)
            load()

        def close(self):
            self.app.dismissed.add(self.run_id)
            self.app.drop(self.name)

    class App:
        def __init__(self):
            self.root = tk.Tk()
            self.root.title("Task Monitor")
            self.root.geometry("1150x720")
            self.root.configure(bg=PANEL)
            style = ttk.Style(self.root)
            style.theme_use("clam")
            style.configure(".", background=PANEL, foreground=FG, fieldbackground=BG)
            style.configure("TNotebook", background=PANEL, borderwidth=0)
            style.configure("TNotebook.Tab", background="#3a3a3a", foreground=FG, padding=(10, 4))
            style.map("TNotebook.Tab", background=[("selected", BG)])
            style.configure("TButton", background="#3a3a3a", foreground=FG, padding=(8, 2))
            style.map("TButton", background=[("active", "#505050"), ("disabled", PANEL)])
            style.configure("Panel.TFrame", background=PANEL)
            style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"))
            style.configure("Info.TLabel", foreground="#9a9a9a", font=("Consolas", 9))
            style.configure("Live.TLabel", background="#252526", foreground="#dcdcaa", font=("Consolas", 10))
            style.configure("TCheckbutton", background=PANEL, foreground=FG)
            bar = ttk.Frame(self.root, style="Panel.TFrame", padding=(8, 4))
            bar.pack(fill="x")
            self.summary = ttk.Label(bar)
            self.summary.pack(side="left")
            ttk.Button(bar, text="Remove finished", command=self.remove_finished).pack(side="right")
            self.follow = tk.BooleanVar(value=True)
            ttk.Checkbutton(bar, text="Jump to new tasks", variable=self.follow).pack(side="right", padx=8)
            self.ontop = tk.BooleanVar(value=False)
            ttk.Checkbutton(bar, text="Always on top", variable=self.ontop,
                            command=lambda: self.root.attributes("-topmost", self.ontop.get())
                            ).pack(side="right", padx=8)
            self.nb = ttk.Notebook(self.root)
            self.nb.pack(fill="both", expand=True)
            self.empty = ttk.Label(self.root, text="No tasks yet.  taskmon run -n NAME -- command ...",
                                   foreground="#7f7f7f")
            self.tabs, self.dismissed, self.started = {}, set(), False

        def drop(self, name):
            tab = self.tabs.pop(name, None)
            if tab:
                self.nb.forget(tab.frame)
                tab.frame.destroy()

        def remove_finished(self):
            for n in remove_finished():
                self.drop(n)

        def tick(self):
            if os.path.exists(quit_flag):  # `taskmon gui --restart`
                self.root.destroy()
                return
            try:
                seen = set()
                for m in all_metas():
                    n = m["name"]
                    seen.add(n)
                    tab = self.tabs.get(n)
                    if tab is None:
                        if m.get("run_id") in self.dismissed:
                            continue
                        tab = self.tabs[n] = Tab(self, m)
                        if self.started and self.follow.get():
                            self.nb.select(tab.frame)
                    elif tab.run_id != m.get("run_id"):
                        tab.reset(m)
                        if self.follow.get():
                            self.nb.select(tab.frame)
                    tab.refresh(m)
                for n in list(self.tabs):
                    if n not in seen:
                        self.drop(n)
                running = sum(1 for t in self.tabs.values() if effective_status(t.meta) == "running")
                self.summary.configure(text=f"{running} running / {len(self.tabs)} tasks     {TASKS}")
                if self.tabs:
                    self.empty.place_forget()
                else:
                    self.empty.place(relx=0.5, rely=0.5, anchor="center")
                if not self.started and self.tabs:  # launched by a run: show the newest task
                    newest = max(self.tabs.values(), key=lambda t: t.meta.get("start", 0))
                    self.nb.select(newest.frame)
                self.started = True
            finally:
                self.root.after(400, self.tick)

    app = App()
    # launched from a background process: Windows won't give us focus, so pop above once
    app.root.lift()
    app.root.attributes("-topmost", True)
    app.root.after(800, lambda: app.root.attributes("-topmost", app.ontop.get()))
    app.tick()
    app.root.mainloop()
    return 0


# ----------------------------------------------------------------- skill

SKILL_MD = """\
---
name: taskmon
description: Run long or noisy commands (builds, test suites, servers, and especially other AI agent CLIs such as `claude -p` or `codex exec`) through taskmon, so the user watches the full live output in an on-screen tabbed window while you receive only a compact summary. Use instead of redirecting output to files or piping through tail/grep.
---

# taskmon

`TM` = `{TM}` (same in Bash and PowerShell). Write it out in full every time.

## Run
```
TM run -n NAME [options] -- COMMAND ARGS...
```
- Anything longer than about a minute: use Bash `run_in_background: true`. You get notified when it exits, and the output is only the summary. Don't poll.
- The exit code is the command's exit code. The GUI opens by itself, with one tab per NAME. Rerunning a finished NAME replaces it. Running names must be unique, so prefix them for parallel work (`fe-build`, `agent-auth`).
- Options: `--sh "a && b"` (run through the shell) · `--cwd DIR` · `--tail N` (default 15) · `--grep REGEX` (repeatable; extra lines to surface) · `--no-default-grep` · `--timeout SEC` · `--input FILE` (stdin, e.g. a long prompt) · `--format auto|text|claude|codex` · `--detach` (return at once, then use `wait`)

## Other AI agents
Have them emit JSON event streams. taskmon auto-detects these, shows the user a readable transcript, and gives you the final result, turn count, cost, tool counts and tool errors:
- `claude -p "PROMPT" --output-format stream-json --verbose [--model ...] [--allowedTools ...]`
- `codex exec --json "PROMPT"`
- For long prompts, write them to a file and pass `--input FILE`; leave the prompt argument out (claude) or use `-` (codex).

## What the summary contains
For text output: the status, exit code, duration, line count, log path, deduplicated error-like lines from before the tail, a warning count and the last N lines. A summary of a successful agent run has no tail.

## Follow-up (only when the summary is not enough)
```
TM summary NAME...          # live status; works while the task is running
TM grep NAME REGEX [-C 2] [-m 30] [--raw]
TM tail NAME [-n 20] [--raw]
TM wait NAME... [--any] [--timeout SEC]   # for --detach; prints the summaries
TM list | TM stop NAME... | TM clean [NAME...]
```
A narrow `grep` is better than a large `tail -n`. Don't Read the full log unless you're asked to or nothing else works.
"""


def tm_invocation():
    """How an agent should call this copy of taskmon."""
    if shutil.which("taskmon"):
        return "taskmon"
    return f"python {os.path.abspath(__file__).replace(os.sep, '/')}"


def cmd_install_skill(a):
    text = SKILL_MD.replace("{TM}", a.invoke or tm_invocation())
    if a.print:
        print(text, end="")
        return 0
    path = os.path.join(os.path.abspath(os.path.expanduser(a.dir)), "taskmon", "SKILL.md")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    print(f"[taskmon] skill written to {path}")
    return 0


# ------------------------------------------------------------------- CLI

def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="taskmon", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"taskmon {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("run", help="run a command under the monitor; prints the summary when done")
    p.add_argument("-n", "--name", help="task / tab name (rerunning a finished name replaces it)")
    p.add_argument("--sh", help="run this command string through the system shell instead")
    p.add_argument("--cwd", help="working directory for the command")
    p.add_argument("--input", help="file fed to the command's stdin (default: no stdin)")
    p.add_argument("--format", default="auto", choices=["auto", "text", "claude", "codex"],
                   help="output parser; auto detects claude stream-json and codex --json")
    p.add_argument("--tail", type=int, default=15, help="lines of tail in the summary (default 15)")
    p.add_argument("--grep", action="append", default=[], help="extra regex to surface (repeatable)")
    p.add_argument("--no-default-grep", action="store_true", help="don't surface error-like lines")
    p.add_argument("--max-matches", type=int, default=25, help="max unique matched lines (default 25)")
    p.add_argument("--width", type=int, default=300, help="clip summary lines to N chars")
    p.add_argument("--result-chars", type=int, default=4000, help="clip agent result text (default 4000)")
    p.add_argument("--timeout", type=float, default=0, help="kill after N seconds (exit 124)")
    p.add_argument("--detach", action="store_true", help="return immediately; use `wait` later")
    p.add_argument("--no-gui", action="store_true", help="don't auto-launch the GUI")
    p.add_argument("command", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("summary", help="print the compact summary (works while running)")
    p.add_argument("names", nargs="+")
    p.set_defaults(fn=cmd_summary)

    p = sub.add_parser("tail", help="last N lines of the readable output")
    p.add_argument("name")
    p.add_argument("-n", type=int, default=20)
    p.add_argument("--raw", action="store_true", help="use the raw log (e.g. JSON events)")
    p.add_argument("--width", type=int, default=300)
    p.set_defaults(fn=cmd_tail)

    p = sub.add_parser("grep", help="search the readable output")
    p.add_argument("name")
    p.add_argument("pattern")
    p.add_argument("-m", type=int, default=30, help="max matches (default 30)")
    p.add_argument("-C", type=int, default=0, help="context lines")
    p.add_argument("--case", action="store_true", help="case sensitive")
    p.add_argument("--raw", action="store_true", help="search the raw log")
    p.add_argument("--width", type=int, default=300)
    p.set_defaults(fn=cmd_grep)

    p = sub.add_parser("wait", help="block until the tasks finish, then print their summaries")
    p.add_argument("names", nargs="+")
    p.add_argument("--timeout", type=float, default=0, help="give up after N seconds (exit 124)")
    p.add_argument("--any", action="store_true", help="return when the first one finishes")
    p.set_defaults(fn=cmd_wait)

    sub.add_parser("list", help="one line per task").set_defaults(fn=cmd_list)

    p = sub.add_parser("stop", help="kill running tasks")
    p.add_argument("names", nargs="+")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("clean", help="delete finished tasks (all, or the named ones)")
    p.add_argument("names", nargs="*")
    p.set_defaults(fn=cmd_clean)

    p = sub.add_parser("gui", help="open the monitor window (one per TASKMON_HOME; closing it never "
                                   "affects running tasks)")
    p.add_argument("--restart", action="store_true",
                   help="ask the open window to close and start a new one (e.g. after upgrading)")
    p.set_defaults(fn=cmd_gui)

    p = sub.add_parser("install-skill", help="install the Claude Code skill that teaches agents to use taskmon")
    p.add_argument("--dir", default="~/.claude/skills",
                   help="skills directory (default ~/.claude/skills; use .claude/skills for one project)")
    p.add_argument("--invoke", help="command agents should use (default: taskmon if on PATH, "
                                    "else python /path/to/taskmon.py)")
    p.add_argument("--print", action="store_true", help="print the skill instead of writing it")
    p.set_defaults(fn=cmd_install_skill)

    a = ap.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
