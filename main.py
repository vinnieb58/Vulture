from adapters.craigslist import search_craigslist


def main() -> None:
    listings = search_craigslist("graphics card", limit=10)

    for listing in listings:
        print(listing)


if __name__ == "__main__":
    main()