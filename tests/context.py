"""Put the plugin's worker modules on the import path for the test suite."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'src' / 'tv-retention' / 'worker'
if str(WORKER) not in sys.path:
    sys.path.insert(0, str(WORKER))
