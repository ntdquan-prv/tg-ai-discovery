"""Who may call the API: the TravelGay backend, with a service token and the admin it calls for.

``Authorization: Bearer <SERVICE_TOKEN>`` proves the caller is the backend; ``X-Admin-Id`` and ``X-Admin-Role``
(``viewer``, ``reviewer`` or ``runner``) say which admin the call is for and are written to the audit log. Outside
production with no ``SERVICE_TOKEN`` set the API is open and acts as a runner named ``dev``, so that Swagger works.
"""

import secrets
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tg_discovery.config import Settings

ROLES = ("viewer", "reviewer", "runner")
bearer = HTTPBearer(
    auto_error=False, description="The service token (SERVICE_TOKEN); not needed in dev while none is configured."
)


@dataclass(frozen=True, slots=True)
class Admin:
    id: str
    role: str


async def current_admin(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    x_admin_id: Annotated[str | None, Header(description="The admin the backend is calling for.")] = None,
    x_admin_role: Annotated[str | None, Header(description="viewer, reviewer or runner.")] = None,
) -> Admin:
    settings: Settings = request.app.state.settings
    token = settings.service_token
    if token is None:
        if settings.app_env == "prod":
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "SERVICE_TOKEN is not configured")
        return Admin(id=x_admin_id or "dev", role=x_admin_role if x_admin_role in ROLES else "runner")
    supplied = credentials.credentials if credentials else ""
    if not secrets.compare_digest(supplied.encode(), token.get_secret_value().encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid or missing service token", headers={"WWW-Authenticate": "Bearer"}
        )
    if not x_admin_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "X-Admin-Id is required")
    if x_admin_role not in ROLES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"X-Admin-Role must be one of {', '.join(ROLES)}")
    return Admin(id=x_admin_id, role=x_admin_role)


def require_role(minimum: str) -> Callable[[Admin], Coroutine[Any, Any, Admin]]:
    """A dependency that lets through admins whose role is ``minimum`` or stronger."""

    async def dependency(admin: Annotated[Admin, Depends(current_admin)]) -> Admin:
        if ROLES.index(admin.role) < ROLES.index(minimum):
            raise HTTPException(status.HTTP_403_FORBIDDEN, f"this needs the {minimum} role")
        return admin

    return dependency


Viewer = Annotated[Admin, Depends(require_role("viewer"))]
Reviewer = Annotated[Admin, Depends(require_role("reviewer"))]
Runner = Annotated[Admin, Depends(require_role("runner"))]
