from __future__ import annotations

import os
from typing import Any

import requests


SEC_USER_AGENT = (os.getenv("SEC_USER_AGENT") or "CardoPraevisio contact@example.com").strip()


def sec_status() -> dict[str, Any]:
    return {
        "provider": "sec_edgar",
        "available": True,
        "api_key_required": False,
        "user_agent_configured": bool(SEC_USER_AGENT),
        "rate_limit_guidance": "10 requests/second",
    }


class SECProvider:
    BASE = "https://data.sec.gov"

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": SEC_USER_AGENT})

    def company_facts(self, cik: str) -> dict[str, Any]:
        cik_padded = str(cik).zfill(10)
        try:
            response = self.session.get(f"{self.BASE}/api/xbrl/companyfacts/CIK{cik_padded}.json", timeout=25)
            if response.status_code >= 400:
                return {"ok": False, "source": "SEC", "error": f"SEC {response.status_code}: {response.text[:200]}", "data": None}
            return {"ok": True, "source": "SEC", "data": response.json()}
        except requests.RequestException as e:
            return {"ok": False, "source": "SEC", "error": f"{type(e).__name__}: {str(e)[:200]}", "data": None}
