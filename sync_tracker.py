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
    """
    Fetches existing roles and maps each to its Notion page_id, 
    date status, and logo/icon status.
    """
    existing = {}
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
                
                # Check if Opening Date column has value populated
                date_prop = props.get("Opening Date", {}).get("date")
                has_date = bool(date_prop and date_prop.get("start"))
                
                # Check if Page Icon (Logo) is already set
                has_icon = bool(page.get("icon"))
                
                identifier = f"{company}|{role}"
                existing[identifier] = {
                    "page_id": page["id"],
                    "has_date": has_date,
                    "has_icon": has_icon
                }
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
    """Evaluates boolean flags, status strings, and UTC date boundaries."""
    now = datetime.now(timezone.utc)
    
    for bool_key in ["isOpen", "is_open", "open", "applicationsOpen"]:
        val = job.get(bool_key)
        if val is False:
            return False

    status_raw = str(job.get("status") or job.get("applicationStatus") or job.get("state") or "").strip().lower()
    negative_keywords = ["closed", "close", "coming", "soon", "upcoming", "unopen", "not_open", "not open", "archived", "draft", "tbd", "paused"]
    if any(neg in status_raw for neg in negative_keywords):
        return False

    opening_date = parse_utc_date(
        job.get("openingDate") or job.get("openDate") or job.get("applicationsOpenDate") or job.get("openedAt") or job.get("startDate")
    )
    closing_date = parse_utc_date(
        job.get("closingDate") or job.get("closeDate") or job.get("applicationsCloseDate") or job.get("closedAt") or job.get("endDate")
    )

    if opening_date and opening_date > now:
        return False

    if closing_date and closing_date < now:
        return False

    if status_raw and "open" not in status_raw:
        return False

    if not opening_date and not status_raw and not any(job.get(k) is True for k in ["isOpen", "is_open", "open"]):
        return False

    return True

def add_notion_row(data_source_id, company, role, link, industry, logo_domain, opening_date_str):
    """Pushes an open role into Notion with properties, opening date, and logo icon."""
    properties = {
        "Company Name": {"title": [{"text": {"content": company}}]},
        "Role / Programme": {"rich_text": [{"text": {"content": role}}]},
        "Job Link": {"url": link if link else None},
        "Industry": {"multi_select": [{"name": industry}]},
        "Status": {"select": {"name": "Not Applied"}}
    }
    
    if opening_date_str:
        properties["Opening Date"] = {"date": {"start": opening_date_str}}

    payload = {
        "parent": {"data_source_id": data_source_id},
        "properties": properties
    }
    
    if logo_domain:
        payload["icon"] = {
            "type": "external",
            "external": {"url": f"https://www.google.com/s2/favicons?domain={logo_domain}&sz=128"}
        }
    
    notion.pages.create(**payload)

def update_existing_role(page_id, opening_date_str=None, logo_domain=None):
    """Patches missing Opening Date and/or missing Icon on existing entries without touching custom fields."""
    update_payload = {}
    
    if opening_date_str:
        update_payload.setdefault("properties", {})["Opening Date"] = {"date": {"start": opening_date_str}}
        
    if logo_domain:
        update_payload["icon"] = {
            "type": "external",
            "external": {"url": f"https://www.google.com/s2/favicons?domain={logo_domain}&sz=128"}
        }
        
    if update_payload:
        notion.pages.update(page_id=page_id, **update_payload)

def sync():
    """Main execution function to fetch Trackr jobs, sync active roles, and backfill missing fields."""
    print("Authenticating and fetching Notion data source ID...")
    data_source_id = get_data_source_id()
    
    existing = get_existing_roles(data_source_id)
    print(f"Found {len(existing)} existing roles in Notion database.")
    
    res = requests.get(TRACKR_API_URL)
    if res.status_code == 200:
        data = res.json()
        jobs_list = data.get("programmes", [])
        added = 0
        updated = 0
        skipped = 0
        
        for job in jobs_list:
            if not is_job_open(job):
                skipped += 1
                continue

            company_info = job.get("company") or {}
            company = company_info.get("name", "Unknown").strip()
            
            role = job.get("name", "").strip()
            link = job.get("url", "")
            
            raw_date = job.get("openingDate") or job.get("openDate") or job.get("applicationsOpenDate") or job.get("openedAt") or job.get("startDate")
            opening_dt = parse_utc_date(raw_date)
            opening_date_str = opening_dt.strftime("%Y-%m-%d") if opening_dt else None
            
            careers_site = company_info.get("careersSite", "")
            logo_domain = get_domain(careers_site) or get_domain(link)
            
            categories = job.get("categories", [])
            industry = categories[0] if categories else job.get("industry", "Finance")
            
            identifier = f"{company.lower()}|{role.lower()}"
            
            if identifier in existing:
                entry = existing[identifier]
                needs_date = not entry["has_date"] and opening_date_str
                needs_icon = not entry["has_icon"] and logo_domain
                
                if needs_date or needs_icon:
                    date_to_pass = opening_date_str if needs_date else None
                    domain_to_pass = logo_domain if needs_icon else None
                    
                    update_existing_role(entry["page_id"], opening_date_str=date_to_pass, logo_domain=domain_to_pass)
                    
                    if needs_date:
                        entry["has_date"] = True
                    if needs_icon:
                        entry["has_icon"] = True
                        
                    updated += 1
                    patched_fields = []
                    if needs_date: patched_fields.append("Date")
                    if needs_icon: patched_fields.append("Logo")
                    print(f"Patched ({', '.join(patched_fields)}): {company} - {role}")
            else:
                add_notion_row(data_source_id, company, role, link, industry, logo_domain, opening_date_str)
                existing[identifier] = {"page_id": None, "has_date": True, "has_icon": bool(logo_domain)}
                added += 1
                print(f"Added New: {company} - {role} (Opened: {opening_date_str})")
                
        print(f"Sync complete. {added} new open roles added, {updated} existing entries backfilled. ({skipped} unopened/closed roles skipped)")
    else:
        print(f"Failed to fetch data from Trackr. Status Code: {res.status_code}")

if __name__ == "__main__":
    sync()

if __name__ == "__main__":
    sync()
