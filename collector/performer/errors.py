"""Exception types shared by the collector."""

from __future__ import annotations

from typing import List, Sequence


class PerformerError(Exception):
    """Base class. The CLI turns these into a message plus exit code 1."""


class BundleError(PerformerError):
    """A bundle could not be read, written or packed."""


class ManifestError(BundleError):
    """A manifest is missing, unparseable or does not satisfy the schema."""

    def __init__(self, message: str, problems: Sequence[str] = ()) -> None:
        self.problems: List[str] = list(problems)
        if self.problems:
            detail = "\n".join(f"  - {p}" for p in self.problems)
            message = f"{message}\n{detail}"
        super().__init__(message)


class PreflightError(PerformerError):
    """The environment cannot support the requested measurement."""
