import requests
from bs4 import BeautifulSoup

url = "https://www.ebay.com/sch/i.html?_nkw=rx+6700+xt"

headers = {
    "User-Agent": "Mozilla/5.0"
}

response = requests.get(url, headers=headers, timeout=20)
response.raise_for_status()

soup = BeautifulSoup(response.text, "lxml")
items = soup.select(".s-item")

print("Found", len(items), "items")

for item in items[:10]:
    title = item.select_one(".s-item__title")
    price = item.select_one(".s-item__price")
    link = item.select_one(".s-item__link")

    if title and price and link:
        print()
        print(title.get_text(strip=True))
        print(price.get_text(strip=True))
        print(link.get("href"))