"""Strict filing predicates shared by the agent and dense sidecar."""
from typing import Any
from pydantic import BaseModel, ConfigDict, Field, field_validator


class FilingFilter(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, strict=True)
    ticker: str = Field(default='', max_length=16, pattern=r'^[A-Z0-9.\-]*$')
    year: int = Field(default=0, ge=0, le=2100)
    section: str = Field(default='', max_length=128, pattern=r'^[^\x00-\x1f\x7f]*$')
    accession: str = Field(default='', pattern=r'^(?:[0-9]{18})?$')

    @field_validator('year')
    @classmethod
    def valid_year(cls, value: int) -> int:
        if value and value < 1993:
            raise ValueError('filing year must be 1993..2100 or 0 for unrestricted')
        return value

    def matches(self, metadata: dict[str, Any]) -> bool:
        return (not self.ticker or self.ticker == metadata.get('Ticker')) and (
            not self.year or self.year == metadata.get('FilingYear')) and (
            not self.section or self.section == metadata.get('SectionItem')) and (
            not self.accession or str(metadata.get('ID', '')).startswith(self.accession + '#'))

    def sql(self) -> tuple[str, list[str | int]]:
        clauses, values = [], []
        for field, value in [('Ticker', self.ticker), ('FilingYear', self.year), ('SectionItem', self.section)]:
            if value:
                clauses.append(f"json_extract(metadata, '$.{field}')=?")
                values.append(value)
        if self.accession:
            clauses.append('id GLOB ?')
            values.append(self.accession + '#*')
        return ' AND '.join(clauses) or '1', values
