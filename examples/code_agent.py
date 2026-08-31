from __future__ import annotations

import json
import sys


KEYWORDS = {"billing"}
message = sys.argv[1].lower()
route = "SUPPORT" if any(keyword in message for keyword in KEYWORDS) else "GENERAL"
print(json.dumps({"route": route}))
