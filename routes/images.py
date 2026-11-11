"""
Image proxy router for Study GT.
Securely streams aptitude test diagrams and charts from Backblaze B2 to the browser.
"""

from fastapi import APIRouter, HTTPException, Response, status
from services.b2_service import b2_service

router = APIRouter(prefix="/api/images", tags=["images"])


@router.get("/{file_path:path}")
async def serve_question_image(file_path: str):
    """
    Proxy and cache question diagrams from Backblaze B2.
    """
    result = await b2_service.get_image(file_path)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Image not found in B2 storage"
        )

    content, mime_type = result
    return Response(
        content=content,
        media_type=mime_type,
        headers={
            "Cache-Control": "public, max-age=86400, immutable",
            "Access-Control-Allow-Origin": "*",
        }
    )

