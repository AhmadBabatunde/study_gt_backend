"""
AI Question Generation Endpoints.
Calls Gemini API utilizing the user's OAuth access token.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from auth import get_current_user, get_user_google_token
from db import fetch_all
from services.ai_service import generate_questions

router = APIRouter(prefix="/api/ai", tags=["ai"])


class GenerateQuestionsRequest(BaseModel):
    category: str = Field(default="Verbal", description="'Verbal' | 'Numerical' | 'Abstract' | 'Mixed'")
    count: int = Field(default=5, ge=1, le=20)
    employer: Optional[str] = Field(default="Dragnet")


@router.post("/generate")
async def generate_ai_questions(
    request: GenerateQuestionsRequest,
    user: dict = Depends(get_current_user),
):
    """
    Generate aptitude questions using Gemini with the authenticated user's Google token.
    Falls back to admin GEMINI_API_KEY if configured.
    """
    user_token = get_user_google_token(user["id"])

    # Grab reference samples from database
    samples = fetch_all(
        """
        SELECT question_id, employer, exam_category, question_type,
               passage, question, options, correct_answer,
               correct_answer_text, explanation, difficulty
        FROM questions
        WHERE (%s = 'Mixed' OR exam_category = %s)
        ORDER BY RANDOM()
        LIMIT 4
        """,
        (request.category, request.category)
    )

    try:
        questions = await generate_questions(
            user_access_token=user_token,
            category=request.category,
            count=request.count,
            sample_questions=samples,
        )
        return {
            "category": request.category,
            "count": len(questions),
            "questions": questions,
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to generate questions: {str(e)}"
        )

