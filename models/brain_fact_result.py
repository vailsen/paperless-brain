# models/brain_fact_result.py
from dataclasses import dataclass, field


@dataclass
class BrainFactResult:
    pbrain_id: str
    text: str
    distance: float = 1.0
    tags: list[str] = field(default_factory=list)
    confidence: float = 1.0
    path: str = ""       # vault-relative path to the .md file
    user: str = ""
