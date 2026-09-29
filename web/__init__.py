"""Web UI for the DSE tools. Puts the repo root on sys.path so the sibling
dse_*.py engine modules import no matter where the server is started from."""
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
