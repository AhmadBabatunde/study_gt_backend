"""Authentication endpoints."""

from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth import (
    create_jwt,
    exchange_google_code,
    get_current_user,
    get_google_user_info,
    get_user_google_token,
    upsert_user,
)
from config import FRONTEND_URL, GOOGLE_CLIENT_ID
from db import execute_returning

router = APIRouter(prefix="/api/auth", tags=["auth"])


class GoogleCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=4096)
    redirect_uri: Optional[str] = None


class DemoLoginRequest(BaseModel):
    name: Optional[str] = "Graduate Candidate"
    email: Optional[str] = "candidate@studygt.ng"


class UpdateProfileRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def _validate_redirect_uri(redirect_uri: str) -> None:
    """
    Validate the redirect_uri sent by the client.

    Two valid cases:
      1. "postmessage" — used by Google GIS popup/ux_mode=popup flows.
         Google's token endpoint accepts this literal string when the auth
         code was obtained via a GIS popup.
      2. A real URL whose origin matches FRONTEND_URL (for redirect flows).
    """
    # GIS popup mode uses the literal string "postmessage"
    if redirect_uri == "postmessage":
        return

    configured = urlparse(FRONTEND_URL)
    requested = urlparse(redirect_uri)

    # Allow localhost with any port during development
    requested_host = requested.hostname or ""
    is_localhost = requested_host in ("localhost", "127.0.0.1")

    if requested.scheme not in {"http", "https"}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="redirect_uri scheme must be http or https",
        )

    if not is_localhost and requested.netloc != configured.netloc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="redirect_uri origin does not match the configured frontend URL",
        )


@router.get("/config")
def get_auth_config():
    """Return public auth config for client SDK setup."""
    is_valid_client_id = (
        bool(GOOGLE_CLIENT_ID)
        and GOOGLE_CLIENT_ID != "your-client-id.apps.googleusercontent.com"
    )
    return {
        "google_client_id": GOOGLE_CLIENT_ID if is_valid_client_id else None,
        "is_configured": is_valid_client_id,
    }


@router.post("/google")
async def sign_in_with_google(request: GoogleCodeRequest):
    """Exchange a Google authorization code and create a Study GT session."""
    redirect_uri = request.redirect_uri or FRONTEND_URL
    _validate_redirect_uri(redirect_uri)

    tokens = await exchange_google_code(request.code, redirect_uri)
    access_token = tokens.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google did not return an access token",
        )

    profile = await get_google_user_info(access_token)
    google_id = profile.get("id")
    email = profile.get("email")
    if not google_id or not email or profile.get("verified_email") is False:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Google account does not provide a verified email address",
        )

    expiry = datetime.now(timezone.utc) + timedelta(
        seconds=int(tokens.get("expires_in", 3600))
    )
    user = upsert_user(
        google_id=google_id,
        email=email,
        name=profile.get("name") or email,
        picture_url=profile.get("picture"),
        access_token=access_token,
        refresh_token=tokens.get("refresh_token"),
        token_expiry=expiry,
    )
    return {
        "access_token": create_jwt(user["id"], user["email"], user["name"] or ""),
        "token_type": "bearer",
        "user": user,
    }


@router.post("/demo")
def demo_login(request: DemoLoginRequest = DemoLoginRequest()):
    """Provide fast demo authentication for testing without OAuth credentials."""
    user = upsert_user(
        google_id=f"demo_{request.email}",
        email=request.email or "candidate@studygt.ng",
        name=request.name or "Graduate Candidate",
        picture_url="https://lh3.googleusercontent.com/aida-public/AB6AXuBbzMmzVSQnQfuCCnB3eZ5DL-pfZrjy1Mydmm3N6A_6iFhcD3H_ZPQAd3FP206jbUbqW9-xaT3_gA6onCU4rhCax-Y7BdndCByR1eZmo1A8QnveVRGsErlz2BbccqjuNpcLlL1adlstedLzAwLUMMKr-msVYi2xt0DZs2_eaPB9CYdtK2QjGorSv_2P1KGSs_HeZqhET9zRcj5qzjK_aBQ4eKgBH90gQ6qPm3Z5alXP",
        access_token="demo_token",
        refresh_token="demo_refresh_token",
        token_expiry=datetime.now(timezone.utc) + timedelta(days=30),
    )
    return {
        "access_token": create_jwt(user["id"], user["email"], user["name"] or ""),
        "token_type": "bearer",
        "user": user,
    }


@router.get("/me")
def read_current_user(user: dict = Depends(get_current_user)):
    """Return the authenticated user's public profile."""
    return user


@router.put("/profile")
def update_profile(
    request: UpdateProfileRequest,
    user: dict = Depends(get_current_user),
):
    """Update the authenticated user's display name."""
    name = request.name.strip()
    if not name:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Name must contain at least one non-whitespace character",
        )
    updated_user = execute_returning(
        """
        UPDATE users
        SET name = %s
        WHERE id = %s
        RETURNING id, google_id, email, name, picture_url
        """,
        (name, user["id"]),
    )
    if not updated_user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return {"user": dict(updated_user)}


@router.post("/refresh")
async def refresh_user_token(user: dict = Depends(get_current_user)):
    """Refresh the user's Google OAuth access token."""
    token = get_user_google_token(user["id"])
    if not token:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unable to refresh Google token"
        )
    return {"status": "ok", "message": "Google OAuth token refreshed successfully"}
