"""Settings from .env. Every demo shortcut is a switch here, not a code path."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    postgres_url: str
    mongo_url: str          # empty = in-memory mongod
    extractor: str          # rules | gemini
    gemini_api_key: str
    as_of_date: date | None  # None = today

    def as_of(self) -> date:
        return self.as_of_date or date.today()


def load_settings() -> Settings:
    load_dotenv(ROOT / '.env', override=False)
    as_of = os.getenv('AS_OF_DATE', '2026-11-20').strip()
    extractor = os.getenv('EXTRACTOR', 'rules').strip() or 'rules'
    if extractor not in ('rules', 'gemini'):
        raise ValueError('EXTRACTOR must be rules or gemini')
    return Settings(postgres_url=os.getenv('POSTGRES_URL', '').strip(),
                    mongo_url=os.getenv('MONGO_URL', '').strip(),
                    extractor=extractor,
                    gemini_api_key=os.getenv('GEMINI_API_KEY', '').strip(),
                    as_of_date=date.fromisoformat(as_of) if as_of else None)
