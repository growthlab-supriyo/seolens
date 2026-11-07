"""The crawl runs here, in its own process, so the Streamlit page never freezes and a crawl survives a browser refresh.

    python -m seolens.worker --run 3 [--resume] [--db path]
"""
import argparse
import os
import subprocess
import sys
import threading
import time
import traceback

from .store import DB_PATH, Store


def start_worker(run_id, resume=False, db=None, finish=False, crux_key=""):
    """Starts the crawler process in the background; returns its PID.
    crux_key: handed to that process in memory only (environment), never written to the database."""
    cmd = [sys.executable, "-m", "seolens.worker", "--run", str(run_id), "--db", str(db or DB_PATH)]
    if resume:
        cmd.append("--resume")
    if finish:
        cmd.append("--finish")
    env = dict(os.environ)
    env.pop("SEOLENS_CRUX_KEY", None)
    if crux_key:
        env["SEOLENS_CRUX_KEY"] = crux_key
    kw = {"cwd": os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True, "env": env}
    if os.name == "nt":
        kw["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, **kw).pid


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


class Reporter:
    """Turns the engine's progress callbacks into rows in SQLite (merged progress + a log)."""

    def __init__(self, store, run_id):
        self.store, self.run_id = store, run_id
        self.progress = dict(store.run(run_id)["progress"] or {})
        self.lock = threading.Lock()
        self.last = 0.0

    def __call__(self, st):
        with self.lock:
            msg = st.pop("log", None) if isinstance(st, dict) else None
            if msg:
                self.store.log(self.run_id, msg)
            self.progress.update(st or {})
            now = time.time()
            if msg or now - self.last > 0.5 or self.progress.get("phase") == "done":
                self.last = now
                self.store.update_run(self.run_id, progress=self.progress)  # also commits the log line


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--finish", action="store_true", help="build the report from the pages saved so far")
    ap.add_argument("--db", default=str(DB_PATH))
    a = ap.parse_args(argv)

    from .audit import Paused, Stopped, run_audit

    store = Store(a.db)
    run = store.run(a.run)
    store.update_run(a.run, status="running", control="finish" if a.finish else "", pid=os.getpid(), error="")
    rep = Reporter(store, a.run)
    try:
        result = run_audit(run["settings"], rep, store=store, run_id=a.run, resume=a.resume)
        store.update_run(a.run, status="done", result=result, control="")
    except Paused:
        store.log(a.run, "⏸ Paused. Everything crawled so far is saved: press Resume to continue.")
        store.update_run(a.run, status="paused", control="")
    except Stopped:
        store.update_run(a.run, status="stopped", control="")
    except Exception as e:  # noqa: BLE001 - the error is shown in the app
        store.log(a.run, "❌ " + str(e))
        store.update_run(a.run, status="failed", control="", error="%s\n\n%s" % (e, traceback.format_exc()[-3000:]))
    finally:
        store.close()


if __name__ == "__main__":
    main()
