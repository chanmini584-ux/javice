"""Write packaging/jarvis.ico from the same drawing the tray uses."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jarvis.desktop.tray import save_ico  # noqa: E402

if __name__ == "__main__":
    print(save_ico(Path(__file__).with_name("jarvis.ico")))
