from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Config:
    rent_url: Optional[str] = None
    sale_url: Optional[str] = None
    target_lat: Optional[float] = None
    target_lon: Optional[float] = None
    out: str = "./Idealista_Property_Analysis.xlsx"
    max_per_type: int = 100
    delay_min: float = 4.0
    delay_max: float = 8.0
    headful: bool = False
    session_file: str = "./.idealista_session.json"
    debug_dir: str = "./debug"
