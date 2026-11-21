"""Stable error codes used by the catalog, operations and citation repository."""

from __future__ import annotations

from typing import Any


class CitationSystemError(Exception):
    """An expected, explainable failure.

    ``code`` is part of the machine-readable interface; callers should not have
    to parse the human message.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "details": self.details,
        }


# Catalog / operation errors
VOLUME_NOT_FOUND = "volume_not_found"
PAGE_NOT_FOUND = "page_not_found"
DUPLICATE_PAGE_ID = "duplicate_page_id"
ANCHOR_NOT_FOUND = "anchor_not_found"
CROSS_VOLUME_MOVE = "cross_volume_move"
CROSS_VOLUME_ANCHOR = "cross_volume_anchor"
INVALID_OPERATION = "invalid_operation"
INVALID_PAGE = "invalid_page"
UNSUPPORTED_OPERATION = "unsupported_operation"

# Resolution errors
RESOLUTION_NOT_FOUND = "resolution_not_found"
RESOLUTION_AMBIGUOUS = "resolution_ambiguous"

# Citation repository errors
CITATION_EXISTS = "citation_exists"
CITATION_NOT_FOUND = "citation_not_found"
INVALID_CITATION = "invalid_citation"
DUPLICATE_PINNED_MEMBER = "duplicate_pinned_member"
CROSS_VOLUME_REFERENCE = "cross_volume_reference"

# Group composition / ordering / print pre-review errors
VOLUME_NAME_INVALID = "volume_name_invalid"
CHAPTER_EXISTS = "chapter_exists"
CHAPTER_NOT_FOUND = "chapter_not_found"
GROUP_NOT_FOUND = "group_not_found"
INVALID_GROUP = "invalid_group"
INVALID_CLAIM = "invalid_claim"
CLAIM_NOT_FOUND = "claim_not_found"
CLAIM_ALREADY_RETRACTED = "claim_already_retracted"
DUPLICATE_CLAIM = "duplicate_claim"
INVALID_CORRECTION = "invalid_correction"
ORDER_CYCLE = "order_cycle"
MISSING_VOLUME_NAME = "missing_volume_name"
BATCH_EXISTS = "batch_exists"
HANDOVER_INVALID = "handover_invalid"


class OperationRejected(CitationSystemError):
    """A whole operation request (batch) was rejected before any write."""


class ReferenceCreationError(CitationSystemError):
    """A citation could not be created; no citation was written."""
