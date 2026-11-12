"""A fixed 500-item sample of the AG News test set (4 balanced topic labels).

Downloaded on first use into data/ (not committed). The sample is seeded, so
every run and both models see the same items.
"""

import csv
import random
import urllib.request

from common import DATA

URL = "https://raw.githubusercontent.com/mhjabreel/CharCnn_Keras/master/data/ag_news_csv/test.csv"
LABELS = {"1": "world", "2": "sports", "3": "business", "4": "sci_tech"}
CRITERIA = {
    "world": "World news, politics, conflicts, diplomacy.",
    "sports": "Sports news.",
    "business": "Business, markets, the economy, companies.",
    "sci_tech": "Science and technology.",
}
INSTRUCTIONS = "Which news category does this article belong to?"


def load(n: int = 500, seed: int = 20261112) -> list[dict]:
    DATA.mkdir(exist_ok=True)
    path = DATA / "ag_news_test.csv"
    if not path.exists():
        urllib.request.urlretrieve(URL, path)
    with path.open(newline="", encoding="utf-8") as f:
        rows = [{"label": LABELS[r[0]], "text": f"{r[1]}\n{r[2]}"} for r in csv.reader(f)]
    random.Random(seed).shuffle(rows)
    return rows[:n]
