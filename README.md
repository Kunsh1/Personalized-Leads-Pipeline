# CAMX 2026 Exhibitor Precise Leads Pipeline

---

## Architecture

```
scraper.py  (Python + Playwright)
  └── exhibitors_raw.json
        └── Upload to Google Drive
              └── n8n Workflow
                    ├── Google Drive → Download JSON
                    ├── Extract From File → Split Out (one item per company)
                    ├── Switch (has_description?)
                    │     ├── YES → Format Row → Sheet 1 (all data)
                    │     │            └── Split in Batches (5)
                    │     │                  └── Build Batch Prompt
                    │     │                        └── Basic LLM Chain (Groq)
                    │     │                              └── Parse + Expand Items
                    │     │                                    └── Wait 2s ──┐
                    │     │                                         (loop back┘)
                    │     │                                    └── Sheet 2 (AI lines)
                    │     └── NO  → Format Row → Sheet 1 (NO_OUTPUT flags)
```

Python handles scraping. n8n handles everything else — enrichment, AI generation, deduplication, and writing to Google Sheets.

---

## Free AI provider

| Provider | Model |
|---|---|
| Groq | llama-3.3-70b-versatile |

Batching 5 companies per call keeps each request under the 8000 TPM limit.
~62 total API calls for 312 companies with descriptions. Takes ~3 minutes.

---

## Step 1 — Run the scraper

```bash
pip install playwright requests beautifulsoup4
playwright install chromium
python scraper.py
# → exhibitors_raw.json
```

The scraper opens 10 concurrent Playwright tabs for speed (~2 mins for 413 pages).

---

## Step 2 — Upload JSON to Google Drive

Upload `exhibitors_raw.json` to Google Drive.
Get the file ID from the share URL:
`https://drive.google.com/file/d/FILE_ID/view`

---

## Step 3 — Set up Google Sheets

Create a new Google Sheet with two tabs:
- `Sheet1 - All Exhibitors`
- `Sheet2 - AI Personalized`

Leave both tabs completely empty — n8n will write the headers automatically on first run.

Note your Sheet ID from the URL:
`https://docs.google.com/spreadsheets/d/SHEET_ID/edit`

---

## Step 4 — Configure n8n

Import `n8n_workflow.json` into n8n (Workflows → Import from file).

Then set the following:

**Google Drive node**
- Credential: Google OAuth2
- File ID: your `CAMX 2026 – Exhibitor AI Enrichment Pipeline.json` file ID from Step 2

**Basic LLM Chain node**
- Sub-node: Groq Chat Model
- Model: `llama-3.3-70b-versatile`
- Free API key: console.groq.com

**All Google Sheets nodes** (3 total)
- Credential: Google OAuth2
- Document ID: your Sheet ID from Step 3

**Split in Batches node**
- Batch size: 5 (keeps requests under Groq's 8000 TPM limit)

---

## Step 5 — Connect the loop

After importing, manually connect the **Wait 2s** output back to **Split in Batches (10)** to close the loop. n8n doesn't preserve this connection on import.

---

## n8n Workflow — Node by Node

| Node | Purpose |
|---|---|
| Manual Trigger | Start the workflow |
| Google Drive – Download JSON | Downloads `exhibitors_raw.json` as binary |
| Extract From File (JSON) | Converts binary → n8n items |
| Split Out | Splits the data array into one item per company |
| Switch | Routes on `has_description` — YES or NO |
| Format Row (has description) | Maps fields, sets `has_description: YES` |
| Format Row (no description) | Maps fields, sets all content fields to `NO_OUTPUT` |
| Sheet 1 → Append or Update (has description) | Writes scraped data row to Sheet 1 |
| Sheet 1 → Append or Update (no description) | Writes flagged row to Sheet 1 |
| Split in Batches (10) | Groups 10 companies per AI call |
| Build Batch Prompt | Aggregates 10 items into one prompt, stores `batch_meta` |
| Basic LLM Chain | Sends prompt to Groq, returns JSON array of 10 lines |
| Parse Response + Expand Items | Maps lines back to companies, expands to 5 items |
| Wait 2s | Rate limit buffer — then loops back to Split in Batches |
| Sheet 2 → AI Personalized | Writes company + personalized line to Sheet 2 |

---

## Google Sheet columns

### Sheet 1 — All Exhibitors (all 413 companies)

| Column | Description |
|---|---|
| exhid | Internal exhibitor ID |
| company_name | Full company name |
| booth | Booth code (e.g. N35) |
| building | Full booth label (e.g. Building C, Level 1 — N35) |
| address | Full address on one line (street, city/zip, country) |
| website | Company website |
| phone | Phone number |
| description | About text from exhibitor page |
| product_categories | Comma-separated category list |
| has_description | YES / NO |
| missing_fields | Comma list of fields the scraper couldn't find |
| detail_url | Source URL |

Missing values show as `NO_OUTPUT`.

### Sheet 2 — AI Personalized (companies with descriptions only)

| Column | Description |
|---|---|
| exhid | Internal exhibitor ID |
| company_name | Full company name |
| booth | Booth code |
| website | Company website |
| description | About text |
| product_categories | Comma-separated categories |
| personalized_line | AI-generated cold email opening line |
| detail_url | Source URL |

Every row in Sheet 2 has a usable personalized line.

---

## Files

| File | Purpose |
|---|---|
| `scraper.py` | Playwright scraper — all 413 exhibitor detail pages |
| `CAMX 2026 – Exhibitor AI Enrichment Pipeline.json` | Import into n8n — full enrichment + AI pipeline |
| `README.md` | This file |
