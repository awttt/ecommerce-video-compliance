"""测试时把 admin/ 放进 sys.path，让 admin_app 可导入。"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
