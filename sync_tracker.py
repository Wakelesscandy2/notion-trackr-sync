import os
import requests
from urllib.parse import urlparse
from notion_client import Client

NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
DATABASE_ID = os.environ.get("DATABASE_ID")
TRACKR_API_URL = "https://api.the-trackr.com/programmes?region=UK&industry=Finance&season=2027&type=summer-internships"

notion = Client(auth=NOTION_TOKEN)

def get_domain(url):
    if not url: return None
    try:
        domain = urlparse(url).netloc
        return domain[4:] if domain.startswith("www.") else domain
    except Exception:
        return None

def get_existing_roles():
    existing = set()
    has_more = True
    next_cursor = None
    
    while has_more:
        kwargs = {"database_id": DATABASE_ID}
        if next_cursor: kwargs["start_cursor"] = next_cursor
        res = notion.databases.query(**kwargs)
        
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

def add_notion_row(company, role, link, industry):
    domain = get_domain(link)
    payload = {
        "parent": {"database_id": DATABASE_ID},
        "properties": {
            "Company Name": {"title": [{"text": {"content": company}}]},
            "Role / Programme": {"rich_text": [{"text": {"content": role}}]},
            "Job Link": {"url": link if link else None},
            "Industry": {"multi_select": [{"name": industry}]},
            "Status": {"select": {"name": "Not Applied"}}
        }
    }
    if domain:
        payload["icon"] = {"type": "external", "external": {"url": f"https://logo.clearbit.com/{domain}"}}
    
    notion.pages.create(**payload)

def sync():
    existing = get_existing_roles()
    print(f"Found {len(existing)} existing roles in Notion.")
    
    res = requests.get(TRACKR_API_URL)
    if res.status_code == 200:
        data = res.json()
        jobs_list = data.get("programmes", [])
        added = 0
        
        for job in jobs_list:
            company_info = job.get("company")
            company = company_info.get("name", "Unknown").strip() if company_info else "Unknown"
            role = job.get("name", "").strip()
            link = job.get("url", "")
            
            categories = job.get("categories", [])
            industry = categories[0] if categories else job.get("industry", "Finance")
            
            identifier = f"{company.lower()}|{role.lower()}"
            if identifier not in existing:
                add_notion_row(company, role, link, industry)
                existing.add(identifier)
                added += 1
                print(f"Added: {company} - {role}")
                
        print(f"Sync complete. {added} new roles added.")
    else:
        print(f"Failed to fetch data. Status Code: {res.status_code}")

if __name__ == "__main__":
    sync()
