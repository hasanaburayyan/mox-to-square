"""PyInstaller entry point: `MtgSync.exe` opens the window; with arguments it runs the CLI."""

import sys

from mtgsync.app import main

if __name__ == "__main__":
    sys.exit(main())
