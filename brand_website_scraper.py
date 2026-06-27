#!/usr/bin/env python3

import argparse
import csv
import html
import re
import sys
import time
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

try:
    import pickle

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    GOOGLE_SHEETS_AVAILABLE = True
except ImportError:
    GOOGLE_SHEETS_AVAILABLE = False


USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SEARCH_URL = "https://html.duckduckgo.com/html/"
SEARCH_IGNORED_DOMAINS = {
    "duckduckgo.com",
    "google.com",
    "bing.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
    "x.com",
    "twitter.com",
    "youtube.com",
    "wikipedia.org",
    "amazon.com",
    "crunchbase.com",
    "bloomberg.com",
    "forbes.com",
    "wixsite.com",
}
COMMON_TLDS = (".com", ".in", ".co", ".net", ".org")
DEFAULT_OUTPUT = "brands_with_websites.csv"
DEFAULT_MISSING_OUTPUT = "brands_without_websites.csv"
BATCH_SIZE = 20

# Optional hardcoded paths.
# If you set INPUT_CSV_PATH, the script will use it when no CLI input path is passed.
# Example:
# INPUT_CSV_PATH = "/Users/rohithborana/Desktop/brands.csv"
# OUTPUT_CSV_PATH = "/Users/rohithborana/Desktop/output.csv"
INPUT_CSV_PATH = "brands-showed-up (1).csv"
OUTPUT_CSV_PATH = ""
START_ROW = 127523
END_ROW = 160934


@dataclass
class MatchResult:
    website: str
    method: str


def normalize_brand(brand: str) -> str:
    return re.sub(r"\s+", " ", brand.strip())


def slugify_brand(brand: str) -> str:
    lowered = brand.lower()
    lowered = lowered.replace("&", " and ")
    lowered = re.sub(r"[^a-z0-9]+", "", lowered)
    return lowered


def fetch_url(url: str, *, method: str = "GET", data: Optional[bytes] = None, timeout: int = 15) -> Tuple[str, str]:
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = response.headers.get_content_charset() or "utf-8"
        body = response.read().decode(content_type, errors="replace")
        final_url = response.geturl()
    return final_url, body


def extract_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def candidate_domain_score(brand: str, domain: str) -> int:
    brand_slug = slugify_brand(brand)
    bare_domain = domain.split(":")[0]
    bare_domain = bare_domain.split(".")[0]
    score = 0
    if bare_domain == brand_slug:
        score += 100
    if brand_slug and brand_slug in domain.replace("-", ""):
        score += 50
    if any(domain.endswith(tld) for tld in COMMON_TLDS):
        score += 10
    if "-" in domain:
        score -= 5
    return score


def is_likely_official_domain(brand: str, domain: str) -> bool:
    if not domain:
        return False
    if any(domain == ignored or domain.endswith(f".{ignored}") for ignored in SEARCH_IGNORED_DOMAINS):
        return False
    brand_slug = slugify_brand(brand)
    normalized_domain = re.sub(r"[^a-z0-9]", "", domain.lower())
    return bool(brand_slug and brand_slug in normalized_domain)


def guess_direct_website(brand: str) -> Optional[MatchResult]:
    slug = slugify_brand(brand)
    if not slug:
        return None

    for tld in COMMON_TLDS:
        url = f"https://www.{slug}{tld}"
        try:
            final_url, body = fetch_url(url, timeout=10)
        except urllib.error.URLError:
            continue
        except Exception:
            continue

        domain = extract_domain(final_url)
        if is_likely_official_domain(brand, domain) and brand.lower().split()[0] in body.lower():
            return MatchResult(website=f"https://{domain}", method="direct_guess")
    return None


def parse_search_result_links(html_text: str) -> List[str]:
    links = []
    for raw_url in re.findall(r'href="(https?://[^"]+)"', html_text):
        links.append(html.unescape(raw_url))
    return links


def search_official_website(brand: str) -> Optional[MatchResult]:
    query = f"{brand} official website"
    payload = urllib.parse.urlencode({"q": query}).encode("utf-8")

    try:
        _, body = fetch_url(SEARCH_URL, method="POST", data=payload, timeout=15)
    except urllib.error.URLError:
        return None
    except Exception:
        return None

    scored_candidates: List[Tuple[int, str]] = []
    for link in parse_search_result_links(body):
        domain = extract_domain(link)
        if not is_likely_official_domain(brand, domain):
            continue
        scored_candidates.append((candidate_domain_score(brand, domain), domain))

    if not scored_candidates:
        return None

    scored_candidates.sort(key=lambda item: item[0], reverse=True)
    best_domain = scored_candidates[0][1]
    return MatchResult(website=f"https://{best_domain}", method="search")


def detect_brand_column(fieldnames: Sequence[str], explicit_column: Optional[str]) -> str:
    if explicit_column:
        if explicit_column not in fieldnames:
            raise ValueError(f"Column '{explicit_column}' was not found in the CSV headers: {', '.join(fieldnames)}")
        return explicit_column

    preferred_names = ("brand", "brand_name", "name", "company", "company_name")
    lowered_map: Dict[str, str] = {name.lower(): name for name in fieldnames}
    for candidate in preferred_names:
        if candidate in lowered_map:
            return lowered_map[candidate]

    if len(fieldnames) == 1:
        return fieldnames[0]

    raise ValueError(
        "Could not detect the brand column automatically. "
        "Pass it with --brand-column."
    )


def read_rows(csv_path: Path) -> Tuple[List[Dict[str, str]], List[str]]:
    with csv_path.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Input CSV has no headers.")
        rows = list(reader)
        return rows, list(reader.fieldnames)


SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TOKEN_FILE = "token.pickle"


def get_sheets_service():
    creds = None
    token_path = Path(TOKEN_FILE)
    if token_path.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def append_to_sheet(sheet_id: str, range_name: str, values: List[List[str]]) -> None:
    if not GOOGLE_SHEETS_AVAILABLE:
        print("Install: pip install google-api-python-client google-auth google-auth-oauthlib", file=sys.stderr)
        return
    service = get_sheets_service()
    service.spreadsheets().values().append(
        spreadsheetId=sheet_id,
        range=range_name,
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": values},
    ).execute()


def write_rows(output_path: Path, rows: Iterable[Dict[str, str]], fieldnames: Sequence[str]) -> None:
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writeheader()
        writer.writerows(rows)


def initialize_output_file(output_path: Path, fieldnames: Sequence[str]) -> None:
    write_rows(output_path, (), fieldnames)


def append_rows(output_path: Path, rows: Iterable[Dict[str, str]], fieldnames: Sequence[str]) -> None:
    with output_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fieldnames))
        writer.writerows(rows)


def slice_rows(rows: List[Dict[str, str]], start_row: int, end_row: Optional[int]) -> List[Dict[str, str]]:
    if start_row < 1:
        raise ValueError("--start-row must be 1 or greater.")
    if end_row is not None and end_row < start_row:
        raise ValueError("--end-row must be greater than or equal to --start-row.")

    start_index = start_row - 1
    end_index = end_row if end_row is not None else None
    return rows[start_index:end_index]


def _lookup_brand(brand: str) -> Tuple[str, Optional[MatchResult]]:
    if not brand:
        return brand, None
    match = guess_direct_website(brand)
    if not match:
        match = search_official_website(brand)
    return brand, match


def scrape_websites(
    rows: List[Dict[str, str]],
    brand_column: str,
    *,
    delay: float,
    output_path: Path,
    missing_output_path: Path,
    sheet_id: str = "",
    batch_size: int = BATCH_SIZE,
    workers: int = 1,
) -> Tuple[int, int]:
    matched_batch: List[Dict[str, str]] = []
    missing_batch: List[Dict[str, str]] = []
    matched_count = 0
    missing_count = 0
    cache: Dict[str, MatchResult] = {}
    misses: set = set()
    cache_lock = Lock()

    def flush_batches() -> None:
        nonlocal matched_count, missing_count
        if matched_batch:
            append_rows(output_path, matched_batch, ("brand", "website", "date"))
            if sheet_id:
                append_to_sheet(sheet_id, "Brands with websites!A:C", [[r["brand"], r["website"], r["date"]] for r in matched_batch])
            matched_count += len(matched_batch)
            matched_batch.clear()
        if missing_batch:
            append_rows(missing_output_path, missing_batch, ("brand", "date"))
            if sheet_id:
                append_to_sheet(sheet_id, "Brands without websites!A:B", [[r["brand"], r["date"]] for r in missing_batch])
            missing_count += len(missing_batch)
            missing_batch.clear()

    ist = timezone(timedelta(hours=5, minutes=30))
    current_date = datetime.now(ist).strftime("%Y-%m-%d")
    total = len(rows)
    if workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            brands = [
                normalize_brand(row.get(brand_column, ""))
                for row in rows
            ]
            futures = {executor.submit(_lookup_brand, b): i for i, b in enumerate(brands, start=1)}
            results: Dict[int, Tuple[str, Optional[MatchResult]]] = {}
            next_idx = 1

            for future in as_completed(futures):
                idx = futures[future]
                results[idx] = future.result()
                while next_idx in results:
                    brand, match = results.pop(next_idx)
                    website = match.website if match else ""
                    if match:
                        matched_batch.append({"brand": brand, "website": website, "date": current_date})
                    else:
                        missing_batch.append({"brand": brand, "date": current_date})
                    print(f"[{next_idx}/{total}] {brand or '(blank)'} -> {website or 'not found'}", file=sys.stderr)
                    if next_idx % batch_size == 0:
                        flush_batches()
                    next_idx += 1
                    if delay > 0:
                        time.sleep(delay)
            flush_batches()
    else:
        for index, row in enumerate(rows, start=1):
            raw_brand = row.get(brand_column, "")
            brand = normalize_brand(raw_brand)
            website = ""
            if brand:
                match = cache.get(brand) if brand not in misses else None
                if match is None:
                    _brand, match = _lookup_brand(brand)
                    if match:
                        cache[brand] = match
                    else:
                        misses.add(brand)
                if match:
                    website = match.website
                    matched_batch.append({"brand": brand, "website": website, "date": current_date})
                else:
                    missing_batch.append({"brand": brand, "date": current_date})

            print(f"[{index}/{total}] {brand or '(blank)'} -> {website or 'not found'}", file=sys.stderr)
            if index % batch_size == 0:
                flush_batches()
            if delay > 0 and index != total:
                time.sleep(delay)

        flush_batches()
    return matched_count, missing_count


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read brand names from a CSV and find likely official websites."
    )
    parser.add_argument("input_csv", nargs="?", help="Path to the input CSV file")
    parser.add_argument(
        "-o",
        "--output",
        default=DEFAULT_OUTPUT,
        help=f"Path to the matched output CSV file (default: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--missing-output",
        default=DEFAULT_MISSING_OUTPUT,
        help=f"Path to the missing output CSV file (default: {DEFAULT_MISSING_OUTPUT})",
    )
    parser.add_argument(
        "--brand-column",
        help="CSV column containing the brand names. If omitted, the script tries to detect it.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Delay in seconds between brands to avoid sending requests too quickly (default: 1.0)",
    )
    parser.add_argument(
        "--start-row",
        type=int,
        help="1-based row number to start from. If omitted, uses START_ROW from the script.",
    )
    parser.add_argument(
        "--end-row",
        type=int,
        help="1-based row number to stop at, inclusive. If omitted, uses END_ROW from the script.",
    )
    parser.add_argument(
        "--all-rows",
        action="store_true",
        help="Process all rows in the CSV and ignore START_ROW, END_ROW, --start-row, and --end-row.",
    )
    parser.add_argument(
        "--google-sheet-id",
        default="",
        help="Google Sheet ID to append results to (requires credentials.json in CWD)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of parallel workers (default: 1, sequential). Set to 5-10 for speed.",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    input_value = args.input_csv or INPUT_CSV_PATH
    output_value = OUTPUT_CSV_PATH or args.output
    missing_output_value = args.missing_output
    start_row = args.start_row if args.start_row is not None else START_ROW
    end_row = args.end_row if args.end_row is not None else END_ROW

    if not input_value:
        print(
            "No input CSV path provided. Set INPUT_CSV_PATH at the top of the script "
            "or pass the CSV path in the command line.",
            file=sys.stderr,
        )
        return 1

    input_path = Path(input_value).expanduser().resolve()
    output_path = Path(output_value).expanduser().resolve()
    missing_output_path = Path(missing_output_value).expanduser().resolve()

    try:
        rows, fieldnames = read_rows(input_path)
        brand_column = detect_brand_column(fieldnames, args.brand_column)
        if args.all_rows:
            rows_to_process = rows
        else:
            rows_to_process = slice_rows(rows, start_row, end_row)
        initialize_output_file(output_path, ("brand", "website", "date"))
        initialize_output_file(missing_output_path, ("brand", "date"))
        matched_count, missing_count = scrape_websites(
            rows_to_process,
            brand_column,
            delay=args.delay,
            output_path=output_path,
            missing_output_path=missing_output_path,
            sheet_id=args.google_sheet_id,
            workers=args.workers,
        )
    except FileNotFoundError:
        print(f"Input CSV not found: {input_path}", file=sys.stderr)
        return 1
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return 1

    print(f"Saved {matched_count} matched results to {output_path}")
    print(f"Saved {missing_count} missing results to {missing_output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
