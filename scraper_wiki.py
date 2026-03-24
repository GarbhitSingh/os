"""
PRESENCE BOT — SCRAPER: WIKIPEDIA
Target: https://en.wikipedia.org/wiki/List_of_reportedly_haunted_locations_in_India
Also:   https://en.wikipedia.org/wiki/List_of_reportedly_haunted_locations

Extracts: location name, description, region, type
Outputs:  data/cases_wiki_raw.json

Run offline, separate from bot.
"""

import requests
import json
import time
import re
import os
from bs4 import BeautifulSoup
from datetime import datetime

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "cases_wiki_raw.json")

HEADERS = {
    "User-Agent": "PresenceBot-Scraper/1.0 (research project; non-commercial)"
}

TARGETS = [
    {
        "url": "https://en.wikipedia.org/wiki/List_of_reportedly_haunted_locations_in_India",
        "country": "India",
        "label": "india"
    },
    {
        "url": "https://en.wikipedia.org/wiki/List_of_reportedly_haunted_locations_in_South_Asia",
        "country": "South Asia",
        "label": "south_asia"
    },
]

# ── Type classifier ──────────────────────────────────────────────────────────

TYPE_KEYWORDS = {
    "disappearance": ["disappear", "vanish", "missing", "lost", "never found"],
    "entity":        ["ghost", "spirit", "apparition", "phantom", "specter", "djinn",
                      "shadow figure", "poltergeist", "entity", "presence", "possession"],
    "signal":        ["sound", "voice", "noise", "whisper", "footstep", "cry",
                      "scream", "music", "echo", "eerie sound"],
    "anomaly":       ["light", "temperature", "cold", "electromagnetic", "infrasound",
                      "hallucination", "unexplained", "anomaly", "strange"],
    "death":         ["murder", "suicide", "death", "died", "killed", "massacre",
                      "tragedy", "haunted after", "victim"],
    "location":      ["fort", "mansion", "building", "hotel", "school", "hospital",
                      "factory", "mine", "bridge", "beach", "well"],
}


def classify_type(text: str) -> str:
    text_lower = text.lower()
    scores = {t: 0 for t in TYPE_KEYWORDS}
    for case_type, keywords in TYPE_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                scores[case_type] += 1
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "unknown"


def assign_tier(text: str, source_type: str) -> tuple[int, str]:
    """Returns (tier, rarity)"""
    text_lower = text.lower()
    high_signals = ["classified", "restricted", "unexplained", "no explanation",
                    "never solved", "bodies found", "multiple deaths", "mass"]
    rare_signals = ["investigated", "documented", "evidence", "recorded",
                    "ips", "paranormal society"]
    if any(s in text_lower for s in high_signals):
        return (3, "anomaly")
    if any(s in text_lower for s in rare_signals):
        return (2, "investigation")
    return (1, "common")


# ── Scraper ──────────────────────────────────────────────────────────────────

def scrape_wiki_page(url: str, country: str) -> list[dict]:
    print(f"[SCRAPER] Fetching: {url}")
    try:
        response = requests.get(url, headers=HEADERS, timeout=15)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"[ERROR] Failed to fetch {url}: {e}")
        return []

    soup = BeautifulSoup(response.text, "html.parser")
    cases = []

    # Wikipedia haunted locations use a consistent pattern:
    # H2/H3 = region/state heading, then <ul><li> entries with bold location name
    current_region = "Unknown"

    for element in soup.find_all(["h2", "h3", "ul"]):
        if element.name in ("h2", "h3"):
            heading_text = element.get_text(strip=True)
            # Skip meta headings
            if heading_text.lower() in ("contents", "references", "see also",
                                         "notes", "external links", "bibliography"):
                continue
            current_region = heading_text.replace("[edit]", "").strip()
            continue

        if element.name == "ul":
            for li in element.find_all("li", recursive=False):
                text = li.get_text(separator=" ", strip=True)
                if len(text) < 30:
                    continue

                # Location name = bold text or text before first colon
                bold = li.find("b")
                if bold:
                    name = bold.get_text(strip=True)
                else:
                    name = text.split(":")[0].strip()
                    if len(name) > 80:
                        name = name[:80]

                if not name or len(name) < 3:
                    continue

                # Description = everything after the name
                description = text
                if ":" in text:
                    parts = text.split(":", 1)
                    description = parts[1].strip() if len(parts) > 1 else text

                case_type = classify_type(text)
                tier, rarity = assign_tier(text, case_type)

                cases.append({
                    "name":        name,
                    "location":    current_region,
                    "country":     country,
                    "type":        case_type,
                    "description": description[:1000],  # cap at 1000 chars
                    "source":      url,
                    "tier":        tier,
                    "rarity":      rarity,
                    "scraped_at":  datetime.utcnow().isoformat()
                })

    print(f"[SCRAPER] Extracted {len(cases)} entries from {url}")
    return cases


def deduplicate(cases: list[dict]) -> list[dict]:
    seen = set()
    unique = []
    for c in cases:
        key = c["name"].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def run():
    all_cases = []

    for target in TARGETS:
        cases = scrape_wiki_page(target["url"], target["country"])
        all_cases.extend(cases)
        time.sleep(2)  # polite delay between requests

    all_cases = deduplicate(all_cases)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(all_cases, f, ensure_ascii=False, indent=2)

    print(f"[SCRAPER] Done. {len(all_cases)} unique cases → {OUTPUT_PATH}")
    return all_cases


if __name__ == "__main__":
    run()
