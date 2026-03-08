from dataclasses import dataclass
from typing import Optional


@dataclass
class Listing:
    source: str
    title: str
    price: Optional[int]
    location: Optional[str]
    link: str