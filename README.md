# Brand Website Scraper

This script reads brand names from a CSV file and tries to find each brand's likely official website.

## What it does

- Reads a CSV with a brand column.
- Tries a direct domain guess first, like `https://www.brandname.com`.
- Falls back to a search query for `brand official website`.
- Writes one CSV for matched rows and one CSV for missing rows.

## Usage

```bash
python3 brand_website_scraper.py input.csv -o output.csv
```

If your brand column is not named `brand`, `brand_name`, `name`, `company`, or `company_name`, pass it explicitly:

```bash
python3 brand_website_scraper.py input.csv -o output.csv --brand-column "Brand Name"
```

To process only a specific row range, like rows 1 through 100:

```bash
python3 brand_website_scraper.py input.csv -o output.csv --start-row 1 --end-row 100
```

You can also set `START_ROW` and `END_ROW` directly at the top of the script if you want a built-in default range.

To ignore the row range and process the entire file:

```bash
python3 brand_website_scraper.py input.csv --all-rows
```

## Input example

```csv
brand
Nike
Adidas
Puma
```

## Output files

- Matched output: `brand`, `website`
- Missing output: `brand`

## Notes

- The script uses live web requests, so it needs internet access when you run it.
- Search pages can change structure over time, so you may want to adjust the parsing logic later if results stop appearing.
- Some brands do not use a domain that closely matches their name, so those may need manual review.
