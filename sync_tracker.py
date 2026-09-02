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

def parse_utc_date(date_val):
    """Safely parses strings, numbers, or ISO timestamps into a UTC-aware datetime object."""
    if not date_val:
        return None
    if isinstance(date_val, (int, float)):
        return datetime.fromtimestamp(date_val / 1000 if date_val > 1e11 else date_val, tz=timezone.utc)
    
    date_str = str(date_val).strip()
    if not date_str:
        return None
        
    date_str_clean = date_str.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(date_str_clean)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                dt = datetime.strptime(date_str[:10], fmt)
                return dt.replace(tzinfo=timezone.utc)
            except Exception:
                pass
    return None

def is_job_open(job):
    """
    Evaluates boolean flags, status strings, and UTC date boundaries to 
    strictly guarantee only currently OPEN roles are added.
    """
    now = datetime.now(timezone.utc)
    
    # 1. Check boolean flags across potential API field names
    for bool_key in ["isOpen", "is_open", "open", "applicationsOpen"]:
        val = job.get(bool_key)
        if val is False:
            return False

    # 2. Reject negative keywords in status strings
    status_raw = str(job.get("status") or job.get("applicationStatus") or job.get("state") or "").strip().lower()
    negative_keywords = ["closed", "close", "coming", "soon", "upcoming", "unopen", "not_open", "not open", "archived", "draft", "tbd", "paused"]
    if any(neg in status_raw for neg in negative_keywords):
        return False

    # 3. Date boundary enforcement
    opening_date = parse_utc_date(job.get("openingDate") or job.get("openDate") or job.get("applicationsOpenDate"))
    closing_date = parse_utc_date(job.get("closingDate") or job.get("closeDate") or job.get("applicationsCloseDate"))

    # Must not open in the future
    if opening_date and opening_date > now:
        return False

    # Must not have closed in the past
    if closing_date and closing_date < now:
        return False

    # 4. Strict requirement: If status string exists, it must explicitly contain 'open'
    if status_raw and "open" not in status_raw:
        return False

    # 5. Default safety check if no dates or status exist
    if not opening_date and not status_raw and not any(job.get(k) is True for k in ["isOpen", "is_open", "open"]):
        return False

    return True

def add_notion_row(data_source_id, company, role, link, industry, logo_domain, opening_date_str):
    """Pushes an open role into Notion with properties, opening date, and logo icon."""
    payload = {
        "parent": {"data_source_id": data_source_id},
        "properties": {
            "Company Name": {"title": [{"text": {"content": company}}]},
            "Role / Programme": {"rich_text": [{"text": {"content": role}}]},
            "Job Link": {"url": link if link else None},
            "Industry": {"multi_select": [{"name": industry}]},
            "Status": {"select": {"name": "Not Applied"}},
            "Opening Date": {"date": {"start": opening_date_str} if opening_date_str else None}
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
            
            # Format parsed opening date as YYYY-MM-DD for Notion
            opening_dt = parse_utc_date(job.get("openingDate") or job.get("openDate") or job.get("applicationsOpenDate"))
            opening_date_str = opening_dt.strftime("%Y-%m-%d") if opening_dt else None
            
            # Use company domain for consistent logo retrieval
            careers_site = company_info.get("careersSite", "")
            logo_domain = get_domain(careers_site) or get_domain(link)
            
            categories = job.get("categories", [])
            industry = categories[0] if categories else job.get("industry", "Finance")
            
            identifier = f"{company.lower()}|{role.lower()}"
            if identifier not in existing:
                add_notion_row(data_source_id, company, role, link, industry, logo_domain, opening_date_str)
                existing.add(identifier)
                added += 1
                print(f"Added: {company} - {role} (Opened: {opening_date_str})")
                
        print(f"Sync complete. {added} new open roles added. ({skipped} unopened/closed roles skipped)")
    else:
        print(f"Failed to fetch data from Trackr. Status Code: {res.status_code}")

if __name__ == "__main__":
    sync()
