"""Entry point for the packaged exe: GUI with no arguments, CLI otherwise."""

from __future__ import annotations

import sys


def main() -> int:
    if sys.stdout is None or sys.stderr is None:
        # Windowed exe: no console. Keep CLI output and tracebacks in a file next to the exe.
        from pathlib import Path
        log = open(Path(sys.executable).resolve().parent / "mtgsync.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log
    if len(sys.argv) > 1:
        from .cli import main as cli_main
        return cli_main()
    from .gui import main as gui_main
    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
