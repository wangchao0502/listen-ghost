"""
main.py
-------
Entry point for listen-ghost.

Responsibilities:
  1. Set Windows DPI awareness so the 400×400 window is physically correct
     on high-DPI displays (125 %, 150 %, 200 % …).
  2. Verify required third-party dependencies and show a friendly error
     if they are missing (before importing anything that would crash).
  3. Set up logging to both console and listen-ghost.log.
  4. Launch the tkinter application.
"""

import sys
import ctypes
import logging
import traceback
from pathlib import Path

# ── Log file sits next to main.py ─────────────────────────────────────────────
LOG_PATH = Path(__file__).parent / 'listen-ghost.log'


def setup_logging() -> None:
    """Write INFO+ to console and DEBUG+ to listen-ghost.log."""
    fmt = '%(asctime)s [%(levelname)s] %(name)s: %(message)s'
    datefmt = '%Y-%m-%d %H:%M:%S'

    logging.basicConfig(
        level=logging.DEBUG,
        format=fmt,
        datefmt=datefmt,
        handlers=[
            logging.FileHandler(LOG_PATH, encoding='utf-8', mode='w'),
            logging.StreamHandler(sys.stdout),
        ],
    )
    logging.getLogger('listen_ghost').setLevel(logging.DEBUG)
    log = logging.getLogger(__name__)
    log.info('listen-ghost starting — log: %s', LOG_PATH)


def _set_dpi_awareness() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def _patch_soundcard() -> None:
    """
    soundcard 0.4.x uses numpy.fromstring (binary mode) which was removed
    in numpy 2.0.  Patch the installed file at runtime if needed.
    """
    try:
        import soundcard  # noqa: F401
        import numpy as np
        if tuple(int(x) for x in np.__version__.split('.')[:2]) < (2, 0):
            return  # no patch needed for numpy < 2.0
        import pathlib
        mf = pathlib.Path(soundcard.__file__).parent / 'mediafoundation.py'
        if not mf.exists():
            return
        src = mf.read_text(encoding='utf-8')
        old = 'numpy.fromstring(_ffi.buffer('
        new = 'numpy.frombuffer(_ffi.buffer('
        if old in src:
            mf.write_text(src.replace(old, new), encoding='utf-8')
            logging.info('Patched soundcard mediafoundation.py (fromstring→frombuffer)')
    except Exception as e:
        logging.warning('soundcard patch failed (non-fatal): %s', e)


def _check_dependencies() -> bool:
    missing = []
    try:
        import soundcard  # noqa: F401
    except ImportError:
        missing.append('soundcard')
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append('numpy')

    if missing:
        pkg_list = ' '.join(missing)
        logging.error('Missing dependencies: %s', pkg_list)
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                'listen-ghost — 缺少依赖',
                f'以下依赖包未安装：\n\n  {pkg_list}\n\n'
                f'请在命令行执行：\n\n  pip install {pkg_list}',
            )
            root.destroy()
        except Exception:
            print(f'[listen-ghost] 缺少依赖，请执行: pip install {pkg_list}', file=sys.stderr)
        return False
    return True


def main() -> None:
    setup_logging()
    log = logging.getLogger(__name__)

    _set_dpi_awareness()
    _patch_soundcard()

    if not _check_dependencies():
        sys.exit(1)

    try:
        from listen_ghost.app import main as run_app
        run_app()
    except Exception:
        log.error('Unhandled exception:\n%s', traceback.format_exc())
        # Also show in a messagebox so windowed users can see it
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror(
                'listen-ghost — 崩溃',
                f'程序崩溃，错误已写入：\n{LOG_PATH}\n\n'
                + traceback.format_exc()[-800:],
            )
            root.destroy()
        except Exception:
            pass
        sys.exit(1)


if __name__ == '__main__':
    main()
