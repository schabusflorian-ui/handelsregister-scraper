"""
crt.sh (Certificate Transparency log) monitor.

Every TLS certificate issued anywhere on the public internet is logged to
Certificate Transparency logs. crt.sh exposes these as a search API. We
poll it for sector-keyword matches ("robot*", "climate*", "*.ai", etc.);
each new domain → one `crt_domain_new` signal.

Why this works for stealth founders: new stealth companies issue a
Let's Encrypt cert for their landing page days-to-weeks before any press
pickup. A sector-keyword match on a certificate issued in the last 90
days with no corresponding activity in older CT logs is a strong "new
company exists" signal.

Rate-limit friendly: ~1 req every 2 seconds (crt.sh is slow but doesn't
hard-rate-limit cooperative clients). Idempotent via the unique
(signal_type, source_url) constraint, so re-runs cost nothing.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Set

from .common import ensure_json, iso_now, normalize_domain

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Query catalogue
# ---------------------------------------------------------------------------

# Pairs of (sector_tag, crt.sh identity pattern). The patterns use % as a
# SQL wildcard — crt.sh is a Postgres-backed service.
#
# Keep this list short — each entry burns ~2–3 seconds of latency. We
# can expand later once we see which patterns yield best.
CRT_QUERIES = [
    ("robotics",  "%robot%"),
    ("robotics",  "%cobot%"),
    ("robotics",  "%robotik%"),
    ("ai",        "%genai%"),
    ("ai",        "%llm%"),
    ("ai",        "%.ai"),             # all .ai domains (noisy, filtered post-hoc)
    ("climate",   "%climate%"),
    ("climate",   "%carbon%"),
    ("climate",   "%cleantech%"),
    ("climate",   "%klimatech%"),
    ("hardware",  "%hardware%"),
    ("hardware",  "%semiconductor%"),
    ("battery",   "%battery%"),
    ("battery",   "%batterie%"),
    ("hydrogen",  "%hydrogen%"),
    ("hydrogen",  "%wasserstoff%"),
    ("solar",     "%solar%"),
    ("space",     "%aerospace%"),
    ("biotech",   "%biotech%"),
    ("mobility",  "%emobility%"),
]


# Hosts/suffixes we always discard — too generic to be meaningful signals.
# These are typically used by CDNs, cloud providers, or domain parkers.
_NOISY_SUFFIXES = (
    ".wordpress.com", ".squarespace.com", ".wixsite.com", ".webflow.io",
    ".github.io", ".netlify.app", ".vercel.app", ".herokuapp.com",
    ".cloudfront.net", ".amazonaws.com", ".azurewebsites.net",
    ".googleusercontent.com", ".cloudflarestorage.com",
    ".shopify.com", ".myshopify.com", ".bigcartel.com",
    ".pages.dev", ".workers.dev", ".fly.dev",
)

_NOISY_SUBSTRINGS = (
    "letsencrypt", "test-", "staging", "dev-", "preview", "sandbox",
    "example.", "localhost", "internal.", "demo.",
)

# A good candidate domain must look brand-y: a short-ish SLD, not a CDN
# path, no numbers padding.
_PLAUSIBLE_DOMAIN = re.compile(r"^[a-z][a-z0-9-]{1,30}\.[a-z]{2,10}(\.[a-z]{2})?$")


# ---------------------------------------------------------------------------
# Fetch + parse
# ---------------------------------------------------------------------------

def _crt_url(pattern: str) -> str:
    """Build the crt.sh URL for one Identity-wildcard query."""
    q = urllib.parse.quote(pattern)
    return f"https://crt.sh/?Identity={q}&exclude=expired&output=json"


def _fetch(url: str, timeout: int = 45, retries: int = 3) -> Optional[list]:
    """GET → parsed JSON list, or None on any error.

    crt.sh is notoriously flaky — 502s and read-timeouts are common under
    load. Retries with linear backoff. Final failure returns None (the
    caller logs + moves on; scheduler will retry on the next tick).
    """
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "handelsregister-scraper/signals (crt.sh monitor)",
            "Accept": "application/json",
        },
    )
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
            try:
                return json.loads(body)
            except (ValueError, json.JSONDecodeError) as e:
                last_err = f"non-json response: {e}"
                return None  # not retriable
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code not in (502, 503, 504, 429):
                break  # non-retriable
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_err = str(e)
        except Exception as e:  # noqa: BLE001
            last_err = f"unexpected: {e}"
            break
        if attempt < retries:
            time.sleep(2 + attempt * 3)  # 5s, 8s
    logger.warning("crt.sh fetch failed after %d tries: %s", retries, last_err)
    return None


def _is_plausible(domain: str) -> bool:
    """Filter out wildcard entries, CDN hosts, and uglier shapes."""
    if not domain:
        return False
    d = domain.lower().strip().strip("*.").strip(".")
    if " " in d or "/" in d or ".." in d:
        return False
    if any(d.endswith(sfx) for sfx in _NOISY_SUFFIXES):
        return False
    if any(ns in d for ns in _NOISY_SUBSTRINGS):
        return False
    # Must look like a brand domain
    if not _PLAUSIBLE_DOMAIN.match(d):
        return False
    # Keep only reasonably short SLDs — long jumbled hostnames are usually
    # auto-generated throwaways
    sld = d.split(".")[0]
    if len(sld) < 3 or len(sld) > 30:
        return False
    return True


def _parse_cert_time(s: Optional[str]) -> Optional[str]:
    """crt.sh returns timestamps like '2026-10-01T12:34:56'. Normalize to ISO timezone.utc."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s[:len(datetime.now().strftime(fmt)) if False else len(s)], fmt)
            return dt.replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return s  # fallback: leave whatever we got


def collect_new_domains(max_days_old: int = 90) -> List[dict]:
    """Hit each CRT_QUERIES pattern, return a list of candidate signals.

    Returns dicts shaped to drop straight into the signals table.
    """
    seen: Set[str] = set()
    out: List[dict] = []
    cutoff = datetime.now(timezone.utc).timestamp() - (max_days_old * 86400)

    for tag, pattern in CRT_QUERIES:
        logger.info("crt.sh query: tag=%s pattern=%s", tag, pattern)
        data = _fetch(_crt_url(pattern))
        if not data:
            time.sleep(2.5)
            continue

        for entry in data:
            # entry fields of interest: name_value (SAN list, newline-separated),
            # entry_timestamp, min_cert_id, issuer_ca_id
            names_blob = entry.get("name_value") or ""
            ts = entry.get("entry_timestamp") or entry.get("not_before")
            if not ts:
                continue
            try:
                ts_epoch = datetime.fromisoformat(
                    ts.replace("Z", "+00:00") if "Z" in ts else ts
                ).replace(tzinfo=timezone.utc).timestamp()
            except (ValueError, TypeError):
                try:
                    ts_epoch = datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")\
                                       .replace(tzinfo=timezone.utc).timestamp()
                except (ValueError, TypeError):
                    continue
            if ts_epoch < cutoff:
                continue

            cert_id = entry.get("min_cert_id") or entry.get("id")

            for raw in names_blob.split("\n"):
                dom = normalize_domain(raw.strip())
                if not dom or not _is_plausible(dom):
                    continue
                if dom in seen:
                    continue
                seen.add(dom)
                out.append({
                    "signal_type":   "crt_domain_new",
                    "observed_at":   _parse_cert_time(ts) or iso_now(),
                    "source_url":    f"https://crt.sh/?id={cert_id}" if cert_id else f"crt://{dom}",
                    "entity_name":   dom.split(".")[0],   # SLD as brand guess
                    "entity_domain": dom,
                    "sector_tags":   [tag],
                    "confidence":    60.0,
                    "raw_payload":   {"issuer_ca_id": entry.get("issuer_ca_id"),
                                      "query_tag": tag,
                                      "query_pattern": pattern,
                                      "cert_id": cert_id},
                })

        # Be polite — crt.sh is slow and lightly-loaded
        time.sleep(2.5)

    logger.info("crt.sh: %d candidate domains across %d queries",
                len(out), len(CRT_QUERIES))
    return out


# ---------------------------------------------------------------------------
# DB write
# ---------------------------------------------------------------------------

def write_signals(conn, candidates: Iterable[dict]) -> dict:
    """Insert candidates into signals via INSERT OR IGNORE."""
    cur = conn.cursor()
    inserted = skipped = 0
    for c in candidates:
        res = cur.execute(
            """INSERT OR IGNORE INTO signals (
                 signal_type, observed_at, source_url,
                 entity_name, entity_domain,
                 sector_tags, confidence, raw_payload
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                c["signal_type"], c["observed_at"], c["source_url"],
                c.get("entity_name"), c.get("entity_domain"),
                ensure_json(c.get("sector_tags")),
                c.get("confidence", 50.0),
                ensure_json(c.get("raw_payload")),
            ),
        )
        if res.rowcount:
            inserted += 1
        else:
            skipped += 1
    conn.commit()
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# CLI / scheduler entry
# ---------------------------------------------------------------------------

def run(conn, max_days_old: int = 90) -> dict:
    """One pass — fetch + write + return stats."""
    cands = collect_new_domains(max_days_old=max_days_old)
    stats = write_signals(conn, cands)
    stats["collected"] = len(cands)
    return stats


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--db", default="handelsregister.db")
    p.add_argument("--days", type=int, default=90)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        print(json.dumps(run(conn, max_days_old=args.days), indent=2))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
