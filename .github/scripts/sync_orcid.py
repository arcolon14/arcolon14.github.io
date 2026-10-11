#!/usr/bin/env python3
"""Sync the _publications collection with an ORCID record.

Works are read from the public ORCID API and their metadata is filled in from
Crossref. Each work becomes one Markdown file in _publications/<year>/.

Re-running the script updates generated fields (title, authors, venue, date,
DOI links) but keeps fields edited by hand: `selected`, `cover`, `abstract`,
`pub_pre`, `pub_post`, and any extra `links`. A preprint that has since been
published is updated in place to point at the published version. Add
`lock: true` to a file's front matter to stop the script from touching it.
Files are never deleted.

Usage: python3 .github/scripts/sync_orcid.py [--orcid ID] [--dry-run]
"""

import argparse
import html
import json
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PUB_DIR = REPO_ROOT / "_publications"
PROFILE = REPO_ROOT / "_data" / "profile.yml"

# Name shown in author lists for the ORCID owner; must match a key in
# _data/authors.yml (with `bold: true`) to be highlighted.
SELF_NAME = "Angel G. Rivera-Colón"
SELF_FAMILY = "rivera-colon"

PREPRINT_BADGE = ' <span class="badge badge-pill badge-publication badge-secondary">Preprint</span>'
GENERATED_LINKS = ("Paper", "Preprint")
USER_AGENT = "orcid-publication-sync (https://github.com/arcolon14/arcolon14.github.io)"


def get_json(url, headers=None, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if attempt == retries - 1:
                raise
        except urllib.error.URLError:
            if attempt == retries - 1:
                raise
        time.sleep(2 ** attempt)


def crossref(doi):
    data = get_json("https://api.crossref.org/works/" + urllib.parse.quote(doi))
    return data["message"] if data else None


def ascii_fold(text):
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


def clean_title(title):
    """Keep italics from Crossref's JATS markup, drop every other tag."""
    title = re.sub(r"<(/?)(?:jats:)?italic>", r"<\1i>", title)
    title = re.sub(r"<(?!/?i>)[^>]+>", "", title)
    return re.sub(r"\s+", " ", html.unescape(title)).strip()


def format_authors(authors, orcid):
    names = []
    for a in authors:
        a_orcid = (a.get("ORCID") or "").rstrip("/").split("/")[-1]
        if a_orcid == orcid or ascii_fold(a.get("family", "")) == SELF_FAMILY:
            names.append(SELF_NAME)
        elif a.get("family"):
            # Crossref mixes "Clara T Rehmann" and "Scott T. Small"; use periods.
            given = re.sub(r"\b([A-Z])\b(?!\.)", r"\1.", a.get("given") or "")
            names.append(" ".join(filter(None, [given, a["family"]])))
        elif a.get("name"):
            names.append(a["name"])
    return names


def date_parts(meta):
    for key in ("published-print", "published-online", "published", "issued"):
        parts = (meta.get(key) or {}).get("date-parts") or [[]]
        if parts[0] and parts[0][0]:
            p = parts[0] + [1, 1]
            return p[0], p[1], p[2]
    return None


def group_dois(group):
    """Return (all DOIs in the group, DOIs that ORCID lists as preprints)."""
    dois, preprints = [], set()
    for summary in group["work-summary"]:
        for ext in (summary.get("external-ids") or {}).get("external-id", []):
            if ext["external-id-type"] != "doi" or ext.get("external-id-relationship") != "self":
                continue
            doi = ext["external-id-value"].lower()
            if doi not in dois:
                dois.append(doi)
            if summary["type"] == "preprint":
                preprints.add(doi)
    return dois, preprints


def resolve_group(group, orcid):
    """Pick the published version of a work (falling back to the preprint)."""
    dois, preprint_dois = group_dois(group)
    # Try likely-published DOIs first so a preprint is only used as a fallback.
    ordered = [d for d in dois if d not in preprint_dois] + [d for d in dois if d in preprint_dois]
    published = preprint = None
    for doi in ordered:
        meta = crossref(doi)
        if meta is None:
            continue
        if meta.get("type") == "posted-content":
            preprint = preprint or (doi, meta)
        else:
            published = (doi, meta)
            break
    if published and not preprint:
        # The loop stopped at the published version; ORCID's label is enough
        # to link the matching preprint without another Crossref lookup.
        preprint = next(((d, None) for d in ordered if d in preprint_dois), None)
    chosen = published or preprint
    if chosen is None:
        print(f"  skipped (no Crossref record): {dois or group['work-summary'][0]['title']['title']['value']}")
        return None

    doi, meta = chosen
    year, month, day = date_parts(meta) or (None, 1, 1)
    if year is None:
        print(f"  skipped (no date): {doi}")
        return None
    if published:
        venue = (meta.get("container-title") or [""])[-1 if meta.get("type") == "book-chapter" else 0]
    else:
        venue = ((meta.get("institution") or [{}])[0]).get("name", "Preprint")

    work = {
        "title": clean_title((meta.get("title") or [""])[0]),
        "date": f"{year:04d}-{month:02d}-{day:02d}",
        "pub": venue,
        "pub_date": str(year),
        "authors": format_authors(meta.get("author", []), orcid),
        "doi": doi,
        "links": {"Paper": f"https://doi.org/{doi}"},
    }
    if published and preprint:
        work["preprint_doi"] = preprint[0]
        work["links"]["Preprint"] = f"https://doi.org/{preprint[0]}"
    elif not published:
        work["pub_last"] = PREPRINT_BADGE
    return work, set(dois)


def read_front_matter(path):
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not match:
        return None, text
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def dump_front_matter(fields, body=""):
    lines = ["---"]
    for key, value in fields.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines += [f"  - {json.dumps(v, ensure_ascii=False)}" for v in value]
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            lines += [f"  {k}: {json.dumps(v, ensure_ascii=False)}" for k, v in value.items()]
        elif isinstance(value, bool):
            lines.append(f"{key}: {str(value).lower()}")
        elif key == "date":
            lines.append(f"{key}: {value}")
        else:
            lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.append("---")
    return "\n".join(lines) + "\n" + body


def slugify(text, max_words=6):
    text = re.sub(r"<[^>]+>|[‐-―]", " ", text)
    words = re.findall(r"[a-z0-9]+", ascii_fold(text))
    return "-".join(words[:max_words]) or "untitled"


def index_existing():
    """Map each DOI in an existing publication file to that file."""
    index = {}
    for path in PUB_DIR.rglob("*.md"):
        fields, _ = read_front_matter(path)
        if not fields:
            continue
        for key in ("doi", "preprint_doi"):
            if fields.get(key):
                index[str(fields[key]).lower()] = path
    return index


def merge(work, path):
    """Combine freshly generated fields with hand-edited ones from `path`."""
    old, body = read_front_matter(path) if path else ({}, "")
    old = old or {}
    if old.get("lock"):
        return None, body
    links = dict(work["links"])
    for name, url in (old.get("links") or {}).items():
        if name not in GENERATED_LINKS:
            links[name] = url
    # A badge the script added is regenerated; one written by hand is kept.
    pub_last = old.get("pub_last") if old.get("pub_last") not in (None, PREPRINT_BADGE) else work.get("pub_last")
    fields = {
        "title": work["title"],
        "date": work["date"],
        "selected": old.get("selected", False),
        "pub_pre": old.get("pub_pre"),
        "pub": work["pub"],
        "pub_post": old.get("pub_post"),
        "pub_date": work["pub_date"],
        "pub_last": pub_last,
        "abstract": old.get("abstract"),
        "cover": old.get("cover"),
        "authors": work["authors"],
        "links": links,
        "doi": work["doi"],
        "preprint_doi": work.get("preprint_doi"),
    }
    return {k: v for k, v in fields.items() if v not in (None, "")}, body


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--orcid", help="ORCID iD (default: `orcid` in _data/profile.yml)")
    parser.add_argument("--dry-run", action="store_true", help="print changes without writing files")
    args = parser.parse_args()

    orcid = args.orcid or yaml.safe_load(PROFILE.read_text(encoding="utf-8"))["orcid"]
    works = get_json(f"https://pub.orcid.org/v3.0/{orcid}/works", {"Accept": "application/json"})
    print(f"ORCID {orcid}: {len(works['group'])} works")

    existing = index_existing()
    created = updated = 0
    for group in works["group"]:
        resolved = resolve_group(group, orcid)
        if resolved is None:
            continue
        work, dois = resolved
        path = next((existing[d] for d in dois if d in existing), None)
        fields, body = merge(work, path)
        if fields is None:
            print(f"  locked: {path.relative_to(REPO_ROOT)}")
            continue
        content = dump_front_matter(fields, body)
        if path is None:
            path = PUB_DIR / work["pub_date"] / f"{work['pub_date']}-{slugify(work['title'])}.md"
            n = 2
            while path.exists():
                path = path.with_name(f"{work['pub_date']}-{slugify(work['title'])}-{n}.md")
                n += 1
            action = "created"
            created += 1
        elif path.read_text(encoding="utf-8") != content:
            action = "updated"
            updated += 1
        else:
            continue
        print(f"  {action}: {path.relative_to(REPO_ROOT)}")
        if not args.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            for d in dois:
                existing[d] = path

    print(f"Done: {created} created, {updated} updated{' (dry run)' if args.dry_run else ''}")


if __name__ == "__main__":
    sys.exit(main())
