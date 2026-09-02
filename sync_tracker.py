import os
import requests
from datetime import datetime, timezone
from urllib.parse import urlparse
from notion_client import Client

# Environment Credentials and Target API
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("DATABASE_ID")
TRACKR_API_URL = "https://api.the-trackr.com/programmes?region=UK&industry=Finance&season=2027&type=summer-internships"

notion = Client(auth=NOTION_TOKEN)

def get_domain(url):
    """Extracts the root domain from a given URL."""
    if not url:
        return None
    try:
        domain = urlparse(url).netloc
        return domain[4:] if domain.startswith("www.") else domain
    except Exception:
        return None

def get_data_source_id():
    """Retrieves the underlying data source ID required by modern Notion API versions."""
    db_info = notion.databases.retrieve(database_id=DATABASE_ID)
    return db_info["data_sources"][0]["id"]

def get_existing_roles(data_source_id):
    """Fetches existing roles from Notion to avoid adding duplicate entries."""
    existing = set()
    has_more = True
    next_cursor = None
    
    while has_more:
        kwargs = {"data_source_id": data_source_id}
        if next_cursor:
            kwargs["start_cursor"] = next_cursor
            
        res = notion.data_sources.query(**kwargs)
        
        for page in res.get("results", []):
            props = page["properties"]
            try:
                company = props["Company Name"]["title"][0]["text"]["content"].strip().lower()
                role = props["Role / Programme"]["rich_text"][0]["text"]["content"].strip().lower()
                existing.add(f"{company}|{role}")
            except (KeyError, IndexError):
                continue
                
        has_more = res.get("has_more", False)
        next_cursor = res.get("next_cursor")
        
    return existing

def is_job_open(job):
    """Evaluates status string and UTC timestamps to ensure only active roles are added."""
    now = datetime.now(timezone.utc)
    
    # 1. Filter out explicitly non-open statuses
    status = (job.get("status") or "").strip().lower()
    if status in ["closed", "coming_soon", "upcoming", "archived"]:
        return False
        
    # 2. Check if the opening date is in the future
    opening_date_str = job.get("openingDate")
    if opening_date_str:
        try:
            opening_date = datetime.fromisoformat(opening_date_str.replace("Z", "+00:00"))
            if opening_date > now:
                return False
        except ValueError:
            pass

    # 3. Check if the closing date has already passed
    closing_date_str = job.get("closingDate")
    if closing_date_str:
        try:
            closing_date = datetime.fromisoformat(closing_date_str.replace("Z", "+00:00"))
            if closing_date < now:
                return False
        except ValueError:
            pass
            
    return True

def add_notion_row(data_source_id, company, role, link, industry, logo_domain, opening_date):
    """Pushes an open role into Notion with properties, date, and logo icon."""
    payload = {
        "parent": {"data_source_id": data_source_id},
        "properties": {
            "Company Name": {"title": [{"text": {"content": company}}]},
            "Role / Programme": {"rich_text": [{"text": {"content": role}}]},
            "Job Link": {"url": link if link else None},
            "Industry": {"multi_select": [{"name": industry}]},
            "Status": {"select": {"name": "Not Applied"}},
            "Opening Date": {"date": {"start": opening_date} if opening_date else None}
        }
    }
    
    if logo_domain:
        payload["icon"] = {
            "type": "external",
            "external": {"url": f"https://www.google.com/s2/favicons?domain={logo_domain}&sz=128"}
        }
    
    notion.pages.create(**payload)

def sync():
    """Main execution function to fetch Trackr jobs and sync active roles into Notion."""
    print("Authenticating and fetching Notion data source ID...")
    data_source_id = get_data_source_id()
    
    existing = get_existing_roles(data_source_id)
    print(f"Found {len(existing)} existing roles in Notion database.")
    
    res = requests.get(TRACKR_API_URL)
    if res.status_code == 200:
        data = res.json()
        jobs_list = data.get("programmes", [])
        added = 0
        skipped = 0
        
        for job in jobs_list:
            if not is_job_open(job):
                skipped += 1
                continue

            company_info = job.get("company") or {}
            company = company_info.get("name", "Unknown").strip()
            
            role = job.get("name", "").strip()
            link = job.get("url", "")
            
            # Format opening date as YYYY-MM-DD for Notion Date property
            opening_date_raw = job.get("openingDate")
            opening_date = opening_date_raw[:10] if opening_date_raw else None
            
            # Use company domain for consistent logo retrieval
            careers_site = company_info.get("careersSite", "")
            logo_domain = get_domain(careers_site) or get_domain(link)
            
            categories = job.get("categories", [])
            industry = categories[0] if categories else job.get("industry", "Finance")
            
            identifier = f"{company.lower()}|{role.lower()}"
            if identifier not in existing:
                add_notion_row(data_source_id, company, role, link, industry, logo_domain, opening_date)
                existing.add(identifier)
                added += 1
                print(f"Added: {company} - {role} (Opened: {opening_date})")
                
        print(f"Sync complete. {added} new open roles added. ({skipped} unopened/closed roles skipped)")
    else:
        print(f"Failed to fetch data from Trackr. Status Code: {res.status_code}")

if __name__ == "__main__":
    sync()
