"""Download a gauge's current USGS stage-discharge rating and write a thinned copy for the page.

    python scripts/fetch_rating.py 01427510

Writes src/data/rating-<site>.json with stage (ft) and flow (cfs) arrays, about every 0.1 ft. The page uses it to
put flows (archived, forecast) on the river-level and flood-stage scale. It is today's rating, so applying it to
2021-2022 flows is approximate: ratings shift as the channel changes.
"""

import json
import sys
import urllib.request
from pathlib import Path

URL = "https://waterdata.usgs.gov/nwisweb/get_ratings?site_no={site}&file_type=exsa"


def main(site: str) -> None:
    text = urllib.request.urlopen(URL.format(site=site), timeout=30).read().decode()
    rating_id = next((line.split('ID="')[1].split('"')[0] for line in text.splitlines() if line.startswith("# //RATING ID=")), None)
    retrieved = next((line.split("RETRIEVED:")[1].strip() for line in text.splitlines() if "RETRIEVED:" in line), None)
    rows = [line.split("\t") for line in text.splitlines() if line and not line.startswith("#")][2:]
    pts = [(float(r[0]), float(r[2])) for r in rows]
    stage, flow = [], []
    for s, q in pts:
        if not stage or s - stage[-1] >= 0.1 - 1e-9 or (s, q) == pts[-1]:
            stage.append(round(s, 2))
            flow.append(round(q, 1))
    out = Path(__file__).resolve().parent.parent / "src" / "data" / f"rating-{site}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"site": site, "rating_id": rating_id, "retrieved": retrieved, "stage_ft": stage, "flow_cfs": flow}))
    print(f"{out}: {len(stage)} points, {stage[0]}-{stage[-1]} ft, {flow[0]:,.0f}-{flow[-1]:,.0f} cfs, rating {rating_id}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "01427510")
