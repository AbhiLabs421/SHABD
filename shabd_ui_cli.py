"""
shabd_ui_cli.py — one-command launcher for the SHABD UI.

Usage
-----

    # 1. Foreground (Ctrl+C to stop). Spells live in `my_spells.py`
    #    in the current directory. Audit log persists to ./shabd-audit.jsonl.
    python -m shabd_ui

    # 2. Background daemon. Logs → shabd-ui.log, pid → shabd-ui.pid
    python -m shabd_ui --daemon

    # 3. Pick port + audit path + import a different spells file.
    python -m shabd_ui --port 9000 \\
                       --audit /var/lib/shabd/audit.jsonl \\
                       --spells /opt/myapp/spells.py

    # 4. Stop the daemon.
    python -m shabd_ui --stop

Design notes
------------

* No Docker, no systemd. Pure Python double-fork daemon (works on
  every Linux box) + a PID file. systemd users can call this script
  with `--foreground` from a unit file if they want.
* Spells file is **optional**. If it doesn't exist the UI still runs
  and you build everything from the browser (the Spell Builder page
  is the entire backend in that case).
* The default audit log path keeps the Grimoire chain (and thus the
  user store) durable across restarts.
* No external dependencies beyond stdlib.
"""
from __future__ import annotations

import argparse
import importlib.util
import logging
import os
import signal
import sys
import time
from pathlib import Path

DEFAULT_PORT = 8080
DEFAULT_BIND = "0.0.0.0"
DEFAULT_AUDIT = "shabd-audit.jsonl"
DEFAULT_SPELLS = "my_spells.py"
DEFAULT_PID = "shabd-ui.pid"
DEFAULT_LOG = "shabd-ui.log"


def _load_spells(app, path: str) -> int:
    """Optional: import a user-supplied Python file that decorates
    spells onto `app`. Returns the number of spells loaded."""
    p = Path(path)
    if not p.exists():
        return 0
    spec = importlib.util.spec_from_file_location(
        "user_spells", p.absolute())
    if not spec or not spec.loader:
        return 0
    module = importlib.util.module_from_spec(spec)
    # Expose `app` so user files can write `app.spell(...)` without
    # re-importing it.
    module.app = app  # type: ignore[attr-defined]
    before = len(app._spells)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    if hasattr(module, "register_spells"):
        module.register_spells(app)
    return len(app._spells) - before


def _build(args: argparse.Namespace):
    from shabd import SHABD
    from shabd_ui import UIServer
    app = SHABD(
        "shabd-ui",
        secret=os.environ.get(
            "SHABD_SECRET", "x" * 32),  # set in prod
        require_auth=False,
        grimoire_log_path=args.audit,
    )
    loaded = _load_spells(app, args.spells)
    ui = UIServer(
        app,
        bind=args.bind, port=args.port,
        force_secure_cookies=bool(args.secure_cookies),
        allow_self_register=not bool(args.no_register),
    )
    return app, ui, loaded


def _print_banner(args: argparse.Namespace, loaded: int) -> None:
    print()
    print("=" * 64)
    print("  🔮  SHABD UI ready")
    print("=" * 64)
    print(f"  URL          : http://{args.bind}:{args.port}/")
    if not loaded:
        print("  Spells       : 0 (build them at /builder)")
    else:
        print(f"  Spells       : {loaded} loaded from {args.spells}")
    print(f"  Audit log    : {args.audit}")
    print("  Auth         : built-in user store")
    print()
    print("  First-time setup:")
    print(f"    open  http://{args.bind}:{args.port}/register")
    print("    register the first account — it becomes superuser")
    print("=" * 64)
    print()


def _maybe_start_fastapi(app, ui, args) -> None:
    """If --fastapi-port is set, start a FastAPI server in a background
    thread sharing the SAME app + ui. Spells built in the UI appear in
    Swagger live. If FastAPI isn't installed, warn and carry on — the
    stdlib UI is unaffected."""
    port = getattr(args, "fastapi_port", 0)
    if not port:
        return
    try:
        import shabd_fastapi
    except Exception as e:
        print(f"  ⚠  --fastapi-port set but FastAPI unavailable: {e}")
        print("     Install with:  pip install fastapi uvicorn")
        return
    if not shabd_fastapi.have_fastapi():
        print("  ⚠  FastAPI not installed; skipping --fastapi-port.")
        print("     Install with:  pip install fastapi uvicorn")
        return
    import threading

    def _run():
        try:
            shabd_fastapi.run(app, ui, host=args.bind, port=port)
        except Exception as e:  # pragma: no cover
            print(f"  ⚠  FastAPI server stopped: {e}")

    threading.Thread(target=_run, daemon=True).start()
    print(f"  FastAPI      : http://{args.bind}:{port}/docs  (Swagger)")


def _maybe_start_studio(app, ui, args) -> None:
    """If --studio-port is set, start the visual Chatbot Studio in a
    background thread sharing the same app + ui (and session store)."""
    port = getattr(args, "studio_port", 0)
    if not port:
        return
    try:
        import shabd_studio
    except Exception as e:
        print(f"  ⚠  --studio-port set but Studio unavailable: {e}")
        return
    import threading

    def _run():
        try:
            shabd_studio.run(ui, bind=args.bind, port=port)
        except Exception as e:  # pragma: no cover
            print(f"  ⚠  Studio server stopped: {e}")

    threading.Thread(target=_run, daemon=True).start()
    print(f"  Studio       : http://{args.bind}:{port}/  "
          "(visual chatbot builder)")


def _serve_foreground(args) -> int:
    app, ui, loaded = _build(args)
    _print_banner(args, loaded)
    _maybe_start_fastapi(app, ui, args)
    _maybe_start_studio(app, ui, args)
    ui.serve()
    return 0


def _daemonize(log_path: str, pid_path: str) -> None:
    """Classic Unix double-fork. Detaches from terminal, redirects
    stdio to a log file, writes a PID file."""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    os.umask(0o022)
    if os.fork() > 0:
        sys.exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    with open(log_path, "ab", buffering=0) as lf:
        os.dup2(lf.fileno(), sys.stdout.fileno())
        os.dup2(lf.fileno(), sys.stderr.fileno())
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, sys.stdin.fileno())
    with open(pid_path, "w") as pf:
        pf.write(str(os.getpid()))


def _stop(pid_path: str) -> int:
    p = Path(pid_path)
    if not p.exists():
        print(f"no pid file at {pid_path} — nothing to stop")
        return 1
    try:
        pid = int(p.read_text().strip())
    except Exception:
        print(f"invalid pid file at {pid_path}")
        return 2
    try:
        os.kill(pid, signal.SIGTERM)
        for _ in range(40):  # up to 4 seconds
            time.sleep(0.1)
            try:
                os.kill(pid, 0)
            except OSError:
                p.unlink(missing_ok=True)
                print(f"stopped pid {pid}")
                return 0
        os.kill(pid, signal.SIGKILL)
        p.unlink(missing_ok=True)
        print(f"force-killed pid {pid}")
        return 0
    except ProcessLookupError:
        p.unlink(missing_ok=True)
        print(f"pid {pid} not running; cleaned up pid file")
        return 0
    except Exception as e:
        print(f"stop failed: {e}")
        return 3


def _status(pid_path: str) -> int:
    p = Path(pid_path)
    if not p.exists():
        print(f"not running (no pid file at {pid_path})")
        return 1
    try:
        pid = int(p.read_text().strip())
        os.kill(pid, 0)
        print(f"running (pid {pid})")
        return 0
    except (OSError, ValueError):
        print(f"stale pid file at {pid_path}")
        return 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m shabd_ui",
        description="Launch the SHABD UI in one command.")
    ap.add_argument("--bind", default=DEFAULT_BIND,
                    help=f"address to bind (default {DEFAULT_BIND})")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"port to listen on (default {DEFAULT_PORT})")
    ap.add_argument("--audit", default=DEFAULT_AUDIT,
                    help=f"Grimoire JSONL path (default {DEFAULT_AUDIT})")
    ap.add_argument("--spells", default=DEFAULT_SPELLS,
                    help=("optional spells file to import "
                          f"(default {DEFAULT_SPELLS})"))
    ap.add_argument("--pid", default=DEFAULT_PID,
                    help=f"pid file path (default {DEFAULT_PID})")
    ap.add_argument("--log", default=DEFAULT_LOG,
                    help=f"daemon log path (default {DEFAULT_LOG})")
    ap.add_argument("--daemon", action="store_true",
                    help="run in background")
    ap.add_argument("--foreground", action="store_true",
                    help="(default) run in foreground")
    ap.add_argument("--stop", action="store_true",
                    help="stop the running daemon")
    ap.add_argument("--status", action="store_true",
                    help="show daemon status")
    ap.add_argument("--secure-cookies", action="store_true",
                    help="set Secure flag on cookies (needs HTTPS)")
    ap.add_argument("--no-register", action="store_true",
                    help="disable self-registration (admin-invite only)")
    ap.add_argument("--fastapi-port", type=int, default=0,
                    help="also serve FastAPI + Swagger on this port "
                         "(needs: pip install fastapi uvicorn)")
    ap.add_argument("--studio-port", type=int, default=0,
                    help="also serve the visual Chatbot Studio on this "
                         "port (shares login with the main UI)")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.stop:
        return _stop(args.pid)
    if args.status:
        return _status(args.pid)

    if Path(args.pid).exists():
        try:
            pid = int(Path(args.pid).read_text().strip())
            os.kill(pid, 0)
            print(f"already running (pid {pid}). "
                  f"Use --stop first.")
            return 4
        except (OSError, ValueError):
            Path(args.pid).unlink(missing_ok=True)

    if args.daemon:
        # Pre-build outside the fork so import errors / port-in-use
        # surface to the user's terminal before detaching.
        app, ui, loaded = _build(args)
        _print_banner(args, loaded)
        print(f"  daemonizing… (log {args.log}, pid {args.pid})\n")
        _daemonize(args.log, args.pid)
        _maybe_start_fastapi(app, ui, args)
        _maybe_start_studio(app, ui, args)
        ui.serve()
        return 0

    # Foreground
    try:
        return _serve_foreground(args)
    except KeyboardInterrupt:
        print("\nstopped.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
