"""兼容常见评测入口的薄封装；实现见 subbatch_ledger.py。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from subbatch_ledger import *  # noqa: F401,F403
from subbatch_ledger import ReleaseLedger, main, run_request  # noqa: F401

if __name__ == "__main__":
    raise SystemExit(main())
