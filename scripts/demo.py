#!/usr/bin/env python3
"""Entry point for the demo launcher; the implementation lives in the launcher package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
