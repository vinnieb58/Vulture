from models.listing import Listing


def matches_rules(listing: Listing, rules: dict) -> bool:
    """Return True if the listing passes all rules defined in the hunt config."""
    if not rules:
        return True

    max_price = rules.get("max_price")
    if max_price is not None:
        if listing.price is None or listing.price > max_price:
            return False

    include_keywords = rules.get("include_keywords") or []
    if include_keywords:
        title_lower = listing.title.lower()
        if not any(kw.lower() in title_lower for kw in include_keywords):
            return False

    exclude_keywords = rules.get("exclude_keywords") or []
    if exclude_keywords:
        title_lower = listing.title.lower()
        if any(kw.lower() in title_lower for kw in exclude_keywords):
            return False

    return True
