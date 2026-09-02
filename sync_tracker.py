import os
import requests
from urllib.parse import urlparse
from notion_client import Client

# Credentials and Endpoints
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("DATABASE_ID")
TRACKR_API_URL = "https://api.the-trackr.com/programmes?region=UK&industry=Finance&season=2027&type=summer-internships"

notion = Client(auth=NOTION_TOKEN)

def get_domain(url):
    """Extracts the root domain to fetch Clearbit logos."""
    if not url:
        return None
    try:
        domain = urlparse(url).netloc
        return domain[4:] if domain.startswith("www.") else domain
    except Exception:
        return None

def get_data_source_id():
    """Retrieves the underlying data source ID from the database container."""
    db_info = notion.databases.retrieve(database_id=DATABASE_ID)
    return db_info["data_sources"][0]["id"]

def get_existing_roles(data_source_id):
    """Fetches all existing roles using the new data_sources endpoint."""
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

def add_notion_row(data_source_id, company, role, link, industry):
    """Pushes a new job opening to Notion using the updated parent container ID."""
    domain = get_domain(link)
    payload = {
        "parent": {"data_source_id": data_source_id},
        "properties": {
            "Company Name": {"title": [{"text": {"content": company}}]},
            "Role / Programme": {"rich_text": [{"text": {"content": role}}]},
            "Job Link": {"url": link if link else None},
            "Industry": {"multi_select": [{"name": industry}]},
            "Status": {"select": {"name": "Not Applied"}}
        }
    }
    
    if domain:
        payload["icon"] = {
            "type": "external",
            "external": {"url": f"https://logo.clearbit.com/{domain}"}
        }
    
    notion.pages.create(**payload)

def sync():
    """Pulls live data from Trackr and pushes new roles to Notion."""
    print("Authenticating and fetching data source ID...")
    data_source_id = get_data_source_id()
    
    existing = get_existing_roles(data_source_id)
    print(f"Found {len(existing)} existing roles in Notion database.")
    
    res = requests.get(TRACKR_API_URL)
    if res.status_code == 200:
        data = res.json()
        jobs_list = data.get("programmes", [])
        added = 0
        
        for job in jobs_list:
            # Extract nested company name
            company_info = job.get("company")
            company = company_info.get("name", "Unknown").strip() if company_info else "Unknown"
            
            # Extract role name and link
            role = job.get("name", "").strip()
            link = job.get("url", "")
            
            # Extract Industry/Category logic
            categories = job.get("categories", [])
            industry = categories[0] if categories else job.get("industry", "Finance")
            
            # Prevent duplicates
            identifier = f"{company.lower()}|{role.lower()}"
            if identifier not in existing:
                add_notion_row(data_source_id, company, role, link, industry)
                existing.add(identifier)
                added += 1
                print(f"Added: {company} - {role}")
                
        print(f"Sync complete. {added} new roles added.")
    else:
        print(f"Failed to fetch data from Trackr. Status Code: {res.status_code}")

if __name__ == "__main__":
    sync()
