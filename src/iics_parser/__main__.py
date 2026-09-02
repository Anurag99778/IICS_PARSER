"""Allow ``python -m iics_parser`` as well as the ``iics-parser`` command.

Pip installs the console script into a Scripts/bin directory that is often not
on PATH (notably on Windows), so this gives a way in that never depends on it.
"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
