import re
from pathlib import Path
from urllib.parse import urljoin

import requests


PATTERNS = [
    "priceToBeat",
    "finalPrice",
    "eventMetadata",
    "Price to Beat",
    "currentPrice",
    "data.chain.link",
    "/api/",
]


def main():
    html = Path("logs/polymarket_page_sample.html").read_text(encoding="utf-8")
    srcs = sorted(set(re.findall(r'src="([^"]+\.js\?dpl=[^"]+)"', html)))
    print("chunks", len(srcs))
    for src in srcs:
        url = urljoin("https://polymarket.com", src)
        try:
            text = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text
        except Exception as exc:
            print("ERR", src, exc)
            continue
        hits = [p for p in PATTERNS if p in text]
        if not hits:
            continue
        print("\n==", src, hits, "len", len(text))
        for pattern in hits:
            idx = text.find(pattern)
            print("--", pattern, text[max(0, idx - 500):idx + 1000])


if __name__ == "__main__":
    main()
