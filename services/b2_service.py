"""
Backblaze B2 Cloud Storage Service for Study GT.
Handles authenticated retrieval of question diagrams and matrices from private B2 buckets.
Includes disk-level caching and resilient DNS fallback.
"""

import os
import time
import logging
from typing import Optional, Tuple
from urllib.parse import urlparse
import httpx

from config import B2_KEY_ID, B2_APP_KEY, B2_BUCKET_NAME

logger = logging.getLogger(__name__)

# Local image cache directory
CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "cache", "images")
os.makedirs(CACHE_DIR, exist_ok=True)

# Known Backblaze B2 egress IP fallback in case of local router DNS resolution failures
B2_DOWNLOAD_FALLBACK_IPS = ["149.137.136.16", "149.137.136.9"]


class B2Service:
    def __init__(self):
        self.auth_token: Optional[str] = None
        self.api_url: Optional[str] = None
        self.download_url: Optional[str] = None
        self.token_expiry: float = 0

    def _is_token_valid(self) -> bool:
        return bool(self.auth_token and time.time() < self.token_expiry - 300)

    async def authorize(self) -> bool:
        """Authenticate with B2 and retrieve fresh auth token and URLs."""
        if self._is_token_valid():
            return True

        if not B2_KEY_ID or not B2_APP_KEY:
            logger.warning("B2 credentials (b2_key_id / b2_app_key) not found in environment.")
            return False

        url = "https://api.backblazeb2.com/b2api/v2/b2_authorize_account"
        async with httpx.AsyncClient(timeout=15.0) as client:
            try:
                resp = await client.get(url, auth=(B2_KEY_ID, B2_APP_KEY))
                if resp.status_code != 200:
                    logger.error(f"B2 authorization failed: {resp.status_code} {resp.text}")
                    return False

                data = resp.json()
                self.auth_token = data.get("authorizationToken")
                self.api_url = data.get("apiUrl")
                self.download_url = data.get("downloadUrl", "https://f005.backblazeb2.com")
                # B2 tokens are typically valid for 24 hours (86400 seconds)
                self.token_expiry = time.time() + 86400
                logger.info(f"B2 authorization successful. Download URL: {self.download_url}")
                return True
            except Exception as e:
                logger.error(f"Error connecting to B2 auth endpoint: {e}")
                return False

    def _get_cache_path(self, clean_path: str) -> str:
        # Sanitize filename for local storage
        safe_name = clean_path.replace("/", "_").replace("\\", "_")
        return os.path.join(CACHE_DIR, safe_name)

    async def get_image(self, file_path: str) -> Optional[Tuple[bytes, str]]:
        """
        Fetch image bytes and content-type.
        Checks local disk cache first; if missing, pulls from Backblaze B2.
        """
        # Strip leading slashes and bucket name if present
        clean_path = file_path.lstrip("/")
        if clean_path.startswith("file/"):
            clean_path = clean_path[5:]
        if clean_path.startswith(f"{B2_BUCKET_NAME}/"):
            clean_path = clean_path[len(B2_BUCKET_NAME) + 1:]

        # 1. Check local disk cache
        cache_path = self._get_cache_path(clean_path)
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "rb") as f:
                    content = f.read()
                mime = "image/png" if clean_path.endswith(".png") else "image/jpeg"
                return content, mime
            except Exception as e:
                logger.warning(f"Failed to read from cache {cache_path}: {e}")

        # 2. Authorize B2
        authorized = await self.authorize()
        if not authorized:
            return None

        # Build download URL
        # Standard format: {download_url}/file/{bucket_name}/{file_path}
        primary_url = f"{self.download_url}/file/{B2_BUCKET_NAME}/{clean_path}"
        headers = {
            "Authorization": self.auth_token,
            "User-Agent": "StudyGT-Simulator/1.0",
        }

        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, verify=False) as client:
            try:
                resp = await client.get(primary_url, headers=headers)
                if resp.status_code == 200:
                    content = resp.content
                    mime = resp.headers.get("content-type", "image/png")
                    self._save_to_cache(cache_path, content)
                    return content, mime
            except Exception as err:
                logger.warning(f"Direct B2 download via domain failed ({err}). Trying direct IP fallback...")

            # 3. Fallback: connect directly via B2 egress IP in case local DNS blocked f005
            parsed = urlparse(self.download_url)
            host_header = parsed.netloc or "f005.backblazeb2.com"

            for fallback_ip in B2_DOWNLOAD_FALLBACK_IPS:
                fallback_url = f"https://{fallback_ip}/file/{B2_BUCKET_NAME}/{clean_path}"
                ip_headers = {
                    "Host": host_header,
                    "Authorization": self.auth_token,
                    "User-Agent": "StudyGT-Simulator/1.0",
                }
                try:
                    resp = await client.get(fallback_url, headers=ip_headers)
                    if resp.status_code == 200:
                        content = resp.content
                        mime = resp.headers.get("content-type", "image/png")
                        self._save_to_cache(cache_path, content)
                        return content, mime
                except Exception as ip_err:
                    logger.debug(f"Fallback to {fallback_ip} failed: {ip_err}")
                    logger.debug(f"Fallback to {fallback_ip} failed: {ip_err}")

        logger.error(f"Failed to download image from B2: {clean_path}")
        return None

    def _save_to_cache(self, path: str, content: bytes) -> None:
        try:
            with open(path, "wb") as f:
                f.write(content)
        except Exception as e:
            logger.warning(f"Failed to write image to cache: {e}")

    @staticmethod
    def to_proxy_url(image_url: Optional[str]) -> Optional[str]:
        """Convert a B2 image URL to the local backend proxy endpoint."""
        if not image_url:
            return None

        # If it's already an internal or relative path
        if image_url.startswith("/api/images/"):
            return image_url

        # Check if it's a B2 URL like https://f005.backblazeb2.com/file/gt-study-pack/dragnet/...
        if "backblazeb2.com" in image_url:
            parsed = urlparse(image_url)
            path = parsed.path.lstrip("/")
            # Path looks like: file/gt-study-pack/dragnet/question_images/NUM_T01_Q01.png
            parts = path.split("/")
            if len(parts) >= 3 and parts[0] == "file":
                # Remove file/bucket_name
                file_rel = "/".join(parts[2:])
                return f"/api/images/{file_rel}"
            # S3-compatible B2 URLs use /{bucket}/{key} instead of /file/{bucket}/{key}.
            if parsed.netloc.startswith("s3.") and len(parts) >= 2 and parts[0] == B2_BUCKET_NAME:
                file_rel = "/".join(parts[1:])
                return f"/api/images/{file_rel}"

        return image_url


b2_service = B2Service()
