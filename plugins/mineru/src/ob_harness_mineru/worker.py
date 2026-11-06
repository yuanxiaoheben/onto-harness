"""Invoke the installed MinerU console entry point in the same Python environment."""

import sys
from importlib.metadata import distribution


def main() -> None:
    entries = [
        entry
        for entry in distribution("mineru").entry_points
        if entry.group == "console_scripts" and entry.name == "mineru-kit"
    ]
    if len(entries) != 1:
        raise RuntimeError("Expected MinerU 4.x mineru-kit entry point")
    sys.argv[0] = "mineru-kit"
    entries[0].load()()


if __name__ == "__main__":
    main()
