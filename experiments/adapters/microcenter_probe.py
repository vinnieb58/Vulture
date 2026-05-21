import requests
from bs4 import BeautifulSoup

url = "https://www.microcenter.com/search/search_results.aspx?Ntt=ssd"

headers = {
    "User-Agent": "Mozilla/5.0"
}

response = requests.get(url, headers=headers, timeout=20)
response.raise_for_status()

print("Status:", response.status_code)
print("Final URL:", response.url)

soup = BeautifulSoup(response.text, "lxml")

# Debug checks
print("Title:", soup.title.get_text(strip=True) if soup.title else "No title")

# Try a few likely selectors
candidates = [
    ".product_wrapper",
    ".productClick",
    ".normal",
    ".search-result-product",
    "li.product_wrapper",
    "a.productClick",
]

for selector in candidates:
    found = soup.select(selector)
    print(f"{selector}: {len(found)}")