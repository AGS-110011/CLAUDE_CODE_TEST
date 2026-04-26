from datetime import date
from typing import Literal, Optional

from pydantic import BaseModel


class Listing(BaseModel):
    id: int
    source_id: str
    type: Literal["Sale", "Rent"]
    link: str
    address: str
    distance_m: Optional[int] = None
    price_eur: int
    size_m2: float
    bedrooms: Optional[int] = None
    bathrooms: Optional[int] = None
    floor: Optional[str] = None
    elevator: Optional[Literal["Yes", "No"]] = None
    condition: Optional[Literal["New", "Renovated", "To renovate"]] = None
    year: Optional[int] = None
    terrace: Optional[Literal["Yes", "No"]] = None
    parking: Optional[Literal["Yes", "No"]] = None
    listing_date: Optional[date] = None
    notes: str = ""
