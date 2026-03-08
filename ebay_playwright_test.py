from playwright.sync_api import sync_playwright

url = "https://www.ebay.com/sch/i.html?_nkw=rx+6700+xt"

checks = [
    "s-item",
    "srp-results",
    "srp-river-results",
    "x-search-results",
    "su-card-container",
    "srp-result",
    "listing",
]

with sync_playwright() as p:
    browser = p.chromium.launch(headless=False)
    page = browser.new_page()

    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)

    print("FINAL URL:", page.url)
    print("PAGE TITLE:", page.title())

    content = page.content()

    print("\nSTRING CHECKS:")
    for check in checks:
        print(f"{check}: {check in content}")

    browser.close()