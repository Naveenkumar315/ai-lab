#!/usr/bin/env python3
"""
find_jobs.py - Day 3 of the AI developer roadmap.

Pulls open roles from the public job-board APIs that Greenhouse, Lever and
Ashby expose, filters them against your profile, scores them, and writes the
new ones to a CSV you can paste into your tracking spreadsheet.

No API keys. No scraping. These endpoints are public and meant to be read.

    pip install requests
    python find_jobs.py            # only roles you have not seen before
    python find_jobs.py --all      # everything that matches, ignore history
    python find_jobs.py --check    # just test which company slugs work

State lives in seen_jobs.json next to this file.
"""

import argparse
import csv
import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
SEEN_FILE = HERE / "seen_jobs.json"
OUT_FILE = HERE / "new_jobs.csv"
TIMEOUT = 20

# ---------------------------------------------------------------------------
# YOUR COMPANY LIST - this is the part that decides whether any of this works.
#
# Find a slug by opening a company's careers page and reading the URL:
#   job-boards.greenhouse.io/SLUG   or boards.greenhouse.io/SLUG  -> greenhouse
#   jobs.lever.co/SLUG                                            -> lever
#   jobs.ashbyhq.com/SLUG                                         -> ashby
#
# If the careers page sits on the company's own domain, open one posting and
# see where the Apply button goes - that usually gives it away. Companies on
# Workday, SmartRecruiters or Naukri have no public API; check those by hand.
#
# Run with --check after every change. Aim for 30-50 companies.
# ---------------------------------------------------------------------------
COMPANIES = [
    # (board, slug, your note)
    # --- verified working ---
    ("ashby", "sardine", "India / remote"),
    ("greenhouse", "gitlab", "remote-first, hires in India"),
    ("greenhouse", "grafanalabs", "remote-first"),
    ("greenhouse", "elastic", "remote-first"),
    ("greenhouse", "celonis", "Germany"),

    # --- add your own below, then run --check ---
    # ("greenhouse", "???", "Chennai"),
    # ("lever", "???", "Bangalore"),
]

# ---------------------------------------------------------------------------
# YOUR PROFILE
# ---------------------------------------------------------------------------

# A role must match one of these in its title, or it is dropped.
TITLE_INCLUDE = [
    "full stack", "fullstack", "full-stack",
    "software engineer", "software developer",
    "backend engineer", "backend developer", "back end",
    "frontend engineer", "frontend developer", "front end",
    "node", "react", "javascript", "typescript", "mern",
    "ai engineer", "applied ai", "llm", "genai",
    "python engineer", "platform engineer", "product engineer",
]

# Dropped regardless of anything else.
TITLE_EXCLUDE = [
    "intern", "graduate", "trainee", "apprentice", "working student",
    "principal", "staff", "director", "head of", "vp ", "vice president",
    "manager", "architect",
    "data scientist", "data engineer", "machine learning research",
    "devops", "sre", "security", "qa ", "sdet", "test engineer",
    "android", "ios ", "mobile", "unity", "game",
    "salesforce", "sap ", "dynamics", "java ", ".net ", "golang", "rust ",
    "c++", "php", "ruby", "scala", "embedded", "firmware",
    "sales", "marketing", "recruiter", "support", "success",
]

# Places you can actually work.
GEO_OK = [
    "chennai", "bangalore", "bengaluru", "hyderabad", "pune", "mumbai",
    "delhi", "gurgaon", "gurugram", "noida", "india",
    "tamil nadu", "tamilnadu", "coimbatore", "madurai", "trichy", "hosur",
    "berlin", "munich", "münchen", "hamburg", "frankfurt", "cologne",
    "germany", "deutschland", "singapore",
]

# "Remote" only helps if their entity is somewhere you can be employed.
# A remote-first company posting "United States (Remote)" cannot hire you.
GEO_BLOCKED = [
    "canada", "united states", "usa", "u.s.", "americas", "latam", "brazil",
    "mexico", "argentina", "colombia", "chile",
    "united kingdom", "ireland", "spain", "portugal", "poland",
    "netherlands", "france", "italy", "romania", "czech", "sweden",
    "norway", "denmark", "finland", "switzerland", "austria", "belgium",
    "emea", "australia", "new zealand", "japan", "korea", "china",
    "philippines", "vietnam", "indonesia", "thailand", "israel", "uae",
]

# Found in the description, each adds to the score.
SKILLS = {
    "node": 3, "react": 3, "typescript": 3, "javascript": 2,
    "mongodb": 3, "express": 2, "angular": 2, "next.js": 2,
    "python": 3, "fastapi": 3, "rest api": 1, "microservice": 2,
    "redis": 1, "sql server": 1,
    "llm": 4, "rag": 4, "openai": 4, "generative ai": 4,
    "langchain": 3, "vector": 3, "embedding": 3, "agent": 3,
    "azure": 2, "docker": 1,
}

# Phrases that suggest they will sponsor, or that remote is genuinely open.
SIGNAL_PATTERNS = {
    "sponsorship": r"(visa sponsor|sponsorship|relocation (support|package|assistance)|work permit)",
    "no_sponsorship": r"(no visa sponsor|not (able|be able) to sponsor|must (already )?have.{0,30}right to work|unable to sponsor)",
    "remote_global": r"(remote[- ](first|friendly)|work from anywhere|fully remote|globally distributed)",
}


# ---------------------------------------------------------------------------
# Fetchers - one per board. Each returns a list of normalised dicts,
# or raises LookupError if the slug is wrong. A bad slug must never look
# like a company with no openings.
# ---------------------------------------------------------------------------

def strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = html.unescape(raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _get(url):
    r = requests.get(url, timeout=TIMEOUT, headers={"User-Agent": "job-finder/1.0"})
    if r.status_code == 404:
        raise LookupError("slug not found (404)")
    r.raise_for_status()
    return r.json()


def fetch_greenhouse(slug):
    data = _get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    if "jobs" not in data:
        raise LookupError("no 'jobs' key - wrong slug")
    return [{
        "title": j.get("title", ""),
        "location": (j.get("location") or {}).get("name", ""),
        "url": j.get("absolute_url", ""),
        "description": strip_html(j.get("content", "")),
        "posted": (j.get("updated_at") or "")[:10],
    } for j in data["jobs"]]


def fetch_lever(slug):
    data = _get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    if not isinstance(data, list):
        raise LookupError("slug not found")
    out = []
    for j in data:
        cat = j.get("categories") or {}
        ts = j.get("createdAt")
        out.append({
            "title": j.get("text", ""),
            "location": cat.get("location", ""),
            "url": j.get("hostedUrl", ""),
            "description": j.get("descriptionPlain") or strip_html(j.get("description", "")),
            "posted": datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d") if ts else "",
        })
    return out


def fetch_ashby(slug):
    data = _get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    if not data.get("jobs"):
        raise LookupError("no jobs returned - wrong slug or empty board")
    return [{
        "title": j.get("title", ""),
        "location": j.get("location", "") or "",
        "url": j.get("jobUrl", "") or j.get("applyUrl", ""),
        "description": j.get("descriptionPlain") or strip_html(j.get("descriptionHtml", "")),
        "posted": (j.get("publishedAt") or "")[:10],
    } for j in data["jobs"]]


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ---------------------------------------------------------------------------
# Filtering and scoring
# ---------------------------------------------------------------------------

def title_ok(title: str) -> bool:
    t = f" {title.lower()} "
    if any(bad in t for bad in TITLE_EXCLUDE):
        return False
    return any(good in t for good in TITLE_INCLUDE)


def location_ok(location: str) -> bool:
    loc = location.lower()
    if any(geo in loc for geo in GEO_OK):
        return True                       # India / Germany / Singapore named
    if any(geo in loc for geo in GEO_BLOCKED):
        return False                      # remote, but in someone else's country
    return any(w in loc for w in ("remote", "anywhere", "global", "distributed"))


def score_job(job) -> tuple[int, list[str]]:
    text = (job["title"] + " " + job["description"]).lower()
    score, hits = 0, []
    for skill, weight in SKILLS.items():
        if skill in text:
            score += weight
            hits.append(skill)

    flags = []
    if re.search(SIGNAL_PATTERNS["no_sponsorship"], text):
        score -= 8
        flags.append("NO-SPONSORSHIP")
    elif re.search(SIGNAL_PATTERNS["sponsorship"], text):
        score += 6
        flags.append("sponsors")
    if re.search(SIGNAL_PATTERNS["remote_global"], text):
        score += 4
        flags.append("remote-first")

    return score, hits + flags


def dedupe(matches):
    """Grafana and friends post one copy per country. Keep the best of each."""
    best = {}
    for j in matches:
        key = (j["company"], re.split(r"[|(]", j["title"])[0].strip().lower())
        if key not in best or j["score"] > best[key]["score"]:
            best[key] = j
    return list(best.values())


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()


def save_seen(seen: set) -> None:
    SEEN_FILE.write_text(json.dumps(sorted(seen), indent=0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ignore seen history")
    ap.add_argument("--check", action="store_true", help="only test the slugs")
    ap.add_argument("--min-score", type=int, default=6)
    args = ap.parse_args()

    seen = set() if args.all else load_seen()
    matches, dead = [], []

    for board, slug, note in COMPANIES:
        try:
            jobs = FETCHERS[board](slug)
        except Exception as exc:
            dead.append(f"  {board}/{slug:<18} FAILED  {exc}")
            continue

        print(f"  {board}/{slug:<18} {len(jobs):>4} open roles   ({note})")
        if args.check:
            continue

        for job in jobs:
            if not job["url"] or job["url"] in seen:
                continue
            if not title_ok(job["title"]) or not location_ok(job["location"]):
                continue
            score, hits = score_job(job)
            if score < args.min_score:
                continue
            job.update(company=slug, score=score, matched=", ".join(hits[:10]))
            matches.append(job)

    if dead:
        print("\nThese slugs did not work - fix or remove them in COMPANIES:")
        print("\n".join(dead))

    if args.check:
        return

    matches = dedupe(matches)
    matches.sort(key=lambda j: j["score"], reverse=True)

    print(f"\n{len(matches)} new matches\n")
    for j in matches[:25]:
        print(f"  [{j['score']:>3}] {j['company']:<14} {j['title'][:52]:<52} {j['location'][:28]}")

    if not matches:
        print("  Nothing. Either your list is too small, your filters are too tight,")
        print("  or you have seen everything already. Try --all.")
        return

    with OUT_FILE.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["score", "company", "title", "location", "posted",
                    "matched_on", "url", "status", "applied_on", "notes"])
        for j in matches:
            w.writerow([j["score"], j["company"], j["title"], j["location"],
                        j["posted"], j["matched"], j["url"], "new", "", ""])

    seen.update(j["url"] for j in matches)
    save_seen(seen)
    print(f"\nWrote {OUT_FILE.name} - paste it into your tracking sheet.")


if __name__ == "__main__":
    sys.exit(main())