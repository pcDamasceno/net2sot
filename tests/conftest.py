import sys
from pathlib import Path

# The tests import the package the same way a plugin in another repository
# does -- "from net2sot.x import y" -- so that both resolve to one copy
# of each module. Put the repository root on the path so this works in a plain
# checkout, without depending on the package having been pip-installed first.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
