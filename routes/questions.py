"""Question bank endpoints."""

from typing import Optional
from fastapi import APIRouter, Depends, Query

from auth import get_current_user
from db import fetch_all
from services.b2_service import b2_service

router = APIRouter(prefix="/api/questions", tags=["questions"])


@router.get("/categories")
def list_categories(user: dict = Depends(get_current_user)):
    """Return available exam categories and their question counts."""
    return fetch_all(
        """
        SELECT exam_category AS category,
               COUNT(*) AS question_count,
               COUNT(*) FILTER (WHERE image_url IS NOT NULL AND image_url <> '') AS image_count
        FROM questions
        WHERE exam_category IS NOT NULL AND exam_category <> ''
        GROUP BY exam_category
        ORDER BY exam_category
        """
    )


@router.get("/employers")
def list_employers(user: dict = Depends(get_current_user)):
    """Return assessment providers present in the question bank."""
    return fetch_all(
        """
        SELECT employer,
               COUNT(*) AS question_count,
               COUNT(*) FILTER (WHERE image_url IS NOT NULL AND image_url <> '') AS image_count,
               ARRAY_REMOVE(ARRAY_AGG(DISTINCT NULLIF(exam_category, '') ORDER BY NULLIF(exam_category, '')), NULL) AS categories
        FROM questions
        WHERE employer IS NOT NULL AND employer <> ''
        GROUP BY employer
        ORDER BY employer
        """
    )


@router.get("/random")
def get_random_questions(
    count: int = Query(default=10, ge=1, le=100),
    category: Optional[str] = Query(default=None, min_length=1, max_length=100),
    employer: Optional[str] = Query(default=None, min_length=1, max_length=100),
    user: dict = Depends(get_current_user),
):
    """Return a random sample from the question bank."""
    rows = fetch_all(
        """
        SELECT question_id, employer, exam_category, question_type, test_no,
               q_no, passage, question, options, correct_answer,
               correct_answer_text, explanation, difficulty, image_url
        FROM questions
        WHERE (%s IS NULL OR exam_category = %s)
        AND (%s IS NULL OR employer = %s)
        ORDER BY RANDOM()
        LIMIT %s
        """,
        (category, category, employer, employer, count),
    )

    for r in rows:
        if r.get("image_url"):
            r["image_url"] = b2_service.to_proxy_url(r["image_url"])

    return rows
