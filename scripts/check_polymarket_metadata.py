import re
import sys

import requests


def main():
    slug = sys.argv[1] if len(sys.argv) > 1 else "btc-updown-15m-1779894900"
    url = f"https://polymarket.com/event/{slug}"
    text = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=20).text
    positions = [m.start() for m in re.finditer(re.escape(slug), text)]
    selected = positions[:10] + positions[-20:]
    print({"slug": slug, "html_len": len(text), "positions": selected, "occurrences": len(positions), "priceToBeat_count": text.count("priceToBeat")})
    for idx in selected:
        ctx = text[max(0, idx - 15000):idx + 50000]
        match = re.search(r'"eventMetadata":\{([^}]*)\}', ctx)
        if match:
            print("metadata_near", idx, match.group(1))
        else:
            print("no metadata near", idx)


if __name__ == "__main__":
    main()
