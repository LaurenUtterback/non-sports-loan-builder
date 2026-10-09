"""Pydantic models for the Closing Binder builder."""

from __future__ import annotations

from datetime import date
from typing import Optional

from pydantic import BaseModel, Field


class BinderPart(BaseModel):
    """A slice of an uploaded PDF: the whole file when no range is given,
    else pages page_from..page_to (1-indexed, inclusive). The auto-sort flow
    splits the signed closing package into sections this way."""

    filename: str = ""
    mime: str = "application/pdf"
    b64: str
    page_from: Optional[int] = None
    page_to: Optional[int] = None


class BinderDoc(BaseModel):
    """One section of the binder. Either a single whole file (the manual
    flow: filename/mime/b64) or a list of parts (the auto-sort flow —
    e.g. several insurance PDFs merged into one "Insurance Documents"
    section, or a page range of the signed closing package)."""

    title: str = ""          # shown in the Table of Contents and title page;
                             # blank falls back to a cleaned-up filename
    filename: str = ""
    mime: str = "application/pdf"
    b64: str = ""
    parts: list[BinderPart] = Field(default_factory=list)


class BinderInfo(BaseModel):
    """Deal-level fields shown on the binder's cover page."""

    borrower_name: str = ""
    loan_amount: Optional[float] = None
    loan_number: str = ""
    closing_date: Optional[date] = None


class BinderRequest(BaseModel):
    info: BinderInfo = Field(default_factory=BinderInfo)
    documents: list[BinderDoc] = Field(default_factory=list)
    # A title page before each document, like the tabs of a physical binder.
    tab_pages: bool = True
