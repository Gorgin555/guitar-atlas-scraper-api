"""
GUITAR ATLAS - Reverb API smoke test
====================================

最低限の疎通確認。`.env` を読み、`/my/account` を叩いて 200 が返れば成功。

Usage:
    cd ~/Desktop/ATLAS/code
    source .venv/bin/activate
    python -m reverb.client_smoke_test
"""
from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from .client import ReverbAPIError, ReverbClient


def main() -> int:
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    try:
        client = ReverbClient.from_env()
    except Exception as e:
        print(f"✗ Failed to initialize client: {e}")
        return 2

    try:
        profile = client.get_profile()
    except ReverbAPIError as e:
        print(f"✗ Reverb API error: {e} (status={e.status_code})")
        return 3
    except Exception as e:
        print(f"✗ Unexpected error: {e!r}")
        return 4

    name = profile.get("first_name") or profile.get("email") or "(unknown)"
    print(f"✓ Reverb API auth OK. Profile: {name}")

    # 軽くサーチも一発投げて雛形動作確認
    print("→ trying a sample search: 'Suhr Classic S Antique'")
    try:
        sample = next(client.search_listings(query="Suhr Classic S Antique", per_page=1, max_pages=1), None)
    except ReverbAPIError as e:
        print(f"✗ search failed: {e}")
        return 5

    if sample:
        title = sample.get("title", "(no title)")
        price = (sample.get("price") or {}).get("display") or "N/A"
        print(f"  → first hit: {title} / {price}")
    else:
        print("  → no listings returned (this might still be OK)")

    print("\n[smoke test passed]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
