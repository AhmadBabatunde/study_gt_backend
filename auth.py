"""
Authentication module.
Handles Google OAuth token exchange, JWT session tokens,
and FastAPI dependency for protected routes.
"""

import jwt
import httpx
import json
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from config import JWT_SECRET, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET
from db import fetch_one, execute_returning

security = HTTPBearer(auto_error=False)

JWT_ALGORITHM = 'HS256'
JWT_EXPIRY_HOURS = 24


def create_jwt(user_id: int, email: str, name: str) -> str:
    """Create a JWT token for a user session."""
    payload = {
        'sub': user_id,
        'email': email,
        'name': name,
        'iat': datetime.now(timezone.utc),
        'exp': datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def decode_jwt(token: str) -> dict:
    """Decode and verify a JWT token."""
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Token has expired'
        )
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Invalid token'
        )


async def exchange_google_code(code: str, redirect_uri: str) -> dict:
    """
    Exchange a Google authorization code for access/refresh tokens.
    Returns dict with: access_token, refresh_token, id_token, expires_in
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            'https://oauth2.googleapis.com/token',
            data={
                'code': code,
                'client_id': GOOGLE_CLIENT_ID,
                'client_secret': GOOGLE_CLIENT_SECRET,
                'redirect_uri': redirect_uri,
                'grant_type': 'authorization_code',
            }
        )

        if resp.status_code != 200:
            detail = resp.json().get('error_description', resp.text)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f'Google token exchange failed: {detail}'
            )

        return resp.json()


async def get_google_user_info(access_token: str) -> dict:
    """Fetch user profile info using the access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            'https://www.googleapis.com/oauth2/v2/userinfo',
            headers={'Authorization': f'Bearer {access_token}'}
        )

        if resp.status_code != 200:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail='Failed to fetch Google user info'
            )

        return resp.json()


async def refresh_google_token(refresh_token: str) -> dict:
    """Use a refresh token to get a new access token."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            'https://oauth2.googleapis.com/token',
            data={
                'client_id': GOOGLE_CLIENT_ID,
                'client_secret': GOOGLE_CLIENT_SECRET,
                'refresh_token': refresh_token,
                'grant_type': 'refresh_token',
            }
        )

        if resp.status_code != 200:
            return None

        return resp.json()


def upsert_user(google_id: str, email: str, name: str, picture_url: str,
                access_token: str, refresh_token: Optional[str],
                token_expiry: Optional[datetime]) -> dict:
    """Create or update a user record. Returns the user dict."""
    row = fetch_one(
        "SELECT id, refresh_token FROM users WHERE google_id = %s",
        (google_id,)
    )

    # Keep existing refresh_token if new one is not provided
    if row and not refresh_token:
        refresh_token = row['refresh_token']

    if row:
        user = execute_returning("""
            UPDATE users SET
                email = %s,
                name = %s,
                picture_url = %s,
                access_token = %s,
                refresh_token = COALESCE(%s, refresh_token),
                token_expiry = %s,
                last_login = NOW()
            WHERE google_id = %s
            RETURNING id, google_id, email, name, picture_url
        """, (email, name, picture_url, access_token,
              refresh_token, token_expiry, google_id))
    else:
        user = execute_returning("""
            INSERT INTO users (google_id, email, name, picture_url,
                               access_token, refresh_token, token_expiry)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id, google_id, email, name, picture_url
        """, (google_id, email, name, picture_url,
              access_token, refresh_token, token_expiry))

    return dict(user)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security)
) -> dict:
    """
    FastAPI dependency that extracts and verifies the current user from JWT.
    Returns the JWT payload dict with keys: sub, email, name.
    """
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='Authentication required'
        )

    payload = decode_jwt(credentials.credentials)

    # Verify user still exists
    user = fetch_one("SELECT id, email, name FROM users WHERE id = %s", (payload['sub'],))
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='User not found'
        )

    return dict(user)


def get_user_google_token(user_id: int) -> Optional[str]:
    """
    Get the user's Google OAuth access token for Gemini API calls.
    Attempts to refresh if expired.
    """
    row = fetch_one(
        "SELECT access_token, refresh_token, token_expiry FROM users WHERE id = %s",
        (user_id,)
    )

    if not row:
        return None

    access_token = row['access_token']
    refresh_token = row['refresh_token']
    expiry = row['token_expiry']

    # Check if token is still valid (with 5-minute buffer)
    if expiry and expiry > datetime.now(timezone.utc) + timedelta(minutes=5):
        return access_token

    # Try to refresh
    if refresh_token:
        import asyncio
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    result = pool.submit(
                        asyncio.run, refresh_google_token(refresh_token)
                    ).result()
            else:
                result = asyncio.run(refresh_google_token(refresh_token))

            if result and 'access_token' in result:
                new_token = result['access_token']
                new_expiry = datetime.now(timezone.utc) + timedelta(
                    seconds=result.get('expires_in', 3600)
                )
                execute_returning(
                    "UPDATE users SET access_token = %s, token_expiry = %s WHERE id = %s RETURNING id",
                    (new_token, new_expiry, user_id)
                )
                return new_token
        except Exception:
            pass

    return access_token  # Return possibly expired token as last resort
