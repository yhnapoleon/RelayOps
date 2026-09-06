"""Domain exceptions raised by service-layer functions.

Routers do NOT translate these into HTTPException by hand — global
exception handlers registered in api/main.py do that, so service code
can stay HTTP-agnostic.

Usage:
    raise NotFoundError("Product not found")
    raise ConflictError("Current draft is already under review")
"""


class DomainError(Exception):
    """Base class for all expected, mappable domain errors."""

    status_code: int = 400

    def __init__(self, detail: str = "") -> None:
        super().__init__(detail or self.__class__.__name__)
        self.detail = detail or self.__class__.__name__


class NotFoundError(DomainError):
    status_code = 404


class ForbiddenError(DomainError):
    status_code = 403


class ValidationError(DomainError):
    status_code = 400


class ConflictError(DomainError):
    status_code = 409


class SystemLockedError(DomainError):
    """Operation rejected because the target is a system-managed entity."""
    status_code = 400
