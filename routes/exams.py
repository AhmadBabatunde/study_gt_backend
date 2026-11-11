"""
Exam lifecycle endpoints for Study GT.
Handles mock exam creation (mixed DB + AI questions), practice mode,
answer submission, proctoring violations, image proxying, and comprehensive results analytics.
"""

import json
import logging
import random
from datetime import date, datetime, timedelta, timezone
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from auth import get_current_user, get_user_google_token
from db import fetch_all, fetch_one, execute, execute_returning, get_cursor
from services.ai_service import generate_questions
from services.b2_service import b2_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/exams", tags=["exams"])


def _calculate_current_streak(activity_dates: List[date], today: date) -> int:
    """Count consecutive activity dates, allowing the streak to continue through today."""
    dates = set(activity_dates)
    anchor = today if today in dates else today - timedelta(days=1)
    if anchor not in dates:
        return 0

    streak = 0
    expected = anchor
    while expected in dates:
        streak += 1
        expected -= timedelta(days=1)
    return streak


class StartExamRequest(BaseModel):
    exam_type: str = Field(default="mock", description="'mock' | 'ai_practice' | 'practice'")
    exam_category: str = Field(default="Mixed", description="'Verbal' | 'Numerical' | 'Abstract' | 'Mixed'")
    employer: Optional[str] = Field(default=None)
    total_questions: int = Field(default=60, ge=1, le=100)
    time_limit_seconds: int = Field(default=3600, ge=60, le=14400)


class SubmitAnswerRequest(BaseModel):
    question_index: int
    selected_answer: Optional[str] = None
    time_spent_ms: Optional[int] = 0


class FlagRequest(BaseModel):
    is_flagged: Optional[bool] = None


class ViolationRequest(BaseModel):
    violation_type: str = Field(default="fullscreen_exit")


def _fetch_db_questions(category: str, employer: Optional[str], count: int) -> List[Dict[str, Any]]:
    """Fetch random questions from the database including question images."""
    if category == "Mixed":
        # Split roughly among Verbal, Numerical, Abstract
        cats = ["Verbal", "Numerical", "Abstract"]
        per_cat = max(1, count // len(cats))
        remainder = count - (per_cat * len(cats))
        questions = []
        for i, c in enumerate(cats):
            take = per_cat + (remainder if i == 0 else 0)
            rows = fetch_all(
                """
                SELECT question_id, employer, exam_category, question_type,
                       passage, question, options, correct_answer,
                       correct_answer_text, explanation, difficulty, image_url
                FROM questions
                WHERE exam_category = %s AND (%s IS NULL OR employer = %s)
                ORDER BY RANDOM()
                LIMIT %s
                """,
                (c, employer, employer, take)
            )
            questions.extend(rows)
        # Keep the selected employer filter when a category has too few questions.
        if len(questions) < count:
            needed = count - len(questions)
            extra = fetch_all(
                """
                SELECT question_id, employer, exam_category, question_type,
                       passage, question, options, correct_answer,
                       correct_answer_text, explanation, difficulty, image_url
                FROM questions
                WHERE (%s IS NULL OR employer = %s)
                  AND NOT (question_id = ANY(%s))
                ORDER BY RANDOM()
                LIMIT %s
                """,
                (employer, employer, [question["question_id"] for question in questions], needed)
            )
            questions.extend(extra)
        return questions[:count]
    else:
        rows = fetch_all(
            """
            SELECT question_id, employer, exam_category, question_type,
                   passage, question, options, correct_answer,
                   correct_answer_text, explanation, difficulty, image_url
            FROM questions
            WHERE exam_category = %s AND (%s IS NULL OR employer = %s)
            ORDER BY RANDOM()
            LIMIT %s
            """,
            (category, employer, employer, count)
        )
        return rows[:count]


@router.post("/start")
async def start_exam(request: StartExamRequest, user: dict = Depends(get_current_user)):
    """
    Start an exam session.
    - 'mock': questions from the selected provider in the question bank
    - 'ai_practice': 100% AI questions
    - 'practice': 100% DB questions
    """
    total = request.total_questions
    db_count = total
    ai_count = 0

    if request.exam_type == "ai_practice":
        db_count = 0
        ai_count = total

    combined_questions: List[Dict[str, Any]] = []

    # 1. Fetch DB questions if needed
    if db_count > 0:
        db_questions = _fetch_db_questions(request.exam_category, request.employer, db_count)
        for q in db_questions:
            opts = q.get("options")
            if isinstance(opts, str):
                try:
                    opts = json.loads(opts)
                except Exception:
                    opts = []
            combined_questions.append({
                "question_id": q.get("question_id"),
                "employer": q.get("employer"),
                "question_text": q.get("question"),
                "passage": q.get("passage"),
                "options": opts,
                "correct_answer": q.get("correct_answer"),
                "correct_answer_text": q.get("correct_answer_text"),
                "explanation": q.get("explanation"),
                "is_ai_generated": False,
                "exam_category": q.get("exam_category") or request.exam_category,
                "image_url": q.get("image_url"),
            })

    # 2. Generate AI questions if needed
    if ai_count > 0:
        user_token = get_user_google_token(user["id"])
        samples = _fetch_db_questions(request.exam_category, request.employer, 5)
        try:
            ai_questions = await generate_questions(
                user_access_token=user_token,
                category=request.exam_category if request.exam_category != "Mixed" else "Mixed Aptitude",
                count=ai_count,
                sample_questions=samples,
            )
            for q in ai_questions:
                combined_questions.append({
                    "question_id": None,
                    "question_text": q.get("question"),
                    "passage": q.get("passage"),
                    "options": q.get("options"),
                    "correct_answer": q.get("correct_answer"),
                    "correct_answer_text": q.get("correct_answer_text"),
                    "explanation": q.get("explanation"),
                    "is_ai_generated": True,
                    "exam_category": request.exam_category,
                    "image_url": None,
                })
        except Exception as e:
            logger.error(f"AI generation failed: {e}. Falling back to DB questions.")
            needed = total - len(combined_questions)
            if needed > 0:
                fallback_qs = _fetch_db_questions(request.exam_category, request.employer, needed)
                for q in fallback_qs:
                    opts = q.get("options")
                    if isinstance(opts, str):
                        try:
                            opts = json.loads(opts)
                        except Exception:
                            opts = []
                    combined_questions.append({
                        "question_id": q.get("question_id"),
                        "employer": q.get("employer"),
                        "question_text": q.get("question"),
                        "passage": q.get("passage"),
                        "options": opts,
                        "correct_answer": q.get("correct_answer"),
                        "correct_answer_text": q.get("correct_answer_text"),
                        "explanation": q.get("explanation"),
                        "is_ai_generated": False,
                        "exam_category": q.get("exam_category") or request.exam_category,
                        "image_url": q.get("image_url"),
                    })

    # Shuffle questions
    random.shuffle(combined_questions)
    actual_total = len(combined_questions)

    # 3. Create exam_sessions record
    session_row = execute_returning(
        """
        INSERT INTO exam_sessions (
            user_id, exam_type, exam_category, employer,
            total_questions, time_limit_seconds, started_at, status
        )
        VALUES (%s, %s, %s, %s, %s, %s, NOW(), 'active')
        RETURNING session_id, user_id, exam_type, exam_category, employer,
                  total_questions, time_limit_seconds, started_at, status, violations, max_violations
        """,
        (user["id"], request.exam_type, request.exam_category, request.employer,
         actual_total, request.time_limit_seconds)
    )

    session_id = session_row["session_id"]

    # 4. Batch insert exam_answers with image_url
    with get_cursor() as cur:
        for idx, q in enumerate(combined_questions, start=1):
            cur.execute(
                """
                INSERT INTO exam_answers (
                    session_id, question_index, question_id, question_text,
                    passage, options, correct_answer, correct_answer_text,
                    explanation, is_ai_generated, is_flagged, image_url
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, FALSE, %s)
                """,
                (
                    session_id,
                    idx,
                    q.get("question_id"),
                    q.get("question_text"),
                    q.get("passage"),
                    json.dumps(q.get("options") or []),
                    q.get("correct_answer") or "A",
                    q.get("correct_answer_text") or "",
                    q.get("explanation") or "",
                    q.get("is_ai_generated", False),
                    q.get("image_url"),
                )
            )

    # Prepare response questions: omit answers if active mock/exam
    client_questions = []
    is_practice = (request.exam_type == "practice")
    for idx, q in enumerate(combined_questions, start=1):
        q_data = {
            "question_index": idx,
            "question_id": q.get("question_id"),
            "employer": q.get("employer"),
            "question_text": q.get("question_text"),
            "passage": q.get("passage"),
            "options": q.get("options"),
            "is_ai_generated": q.get("is_ai_generated", False),
            "is_flagged": False,
            "selected_answer": None,
            "image_url": b2_service.to_proxy_url(q.get("image_url")),
        }
        if is_practice:
            q_data["correct_answer"] = q.get("correct_answer")
            q_data["correct_answer_text"] = q.get("correct_answer_text")
            q_data["explanation"] = q.get("explanation")
        client_questions.append(q_data)

    return {
        "session": dict(session_row),
        "questions": client_questions,
    }


@router.get("/history")
def get_exam_history(user: dict = Depends(get_current_user)):
    """Return user's exam attempts history."""
    return fetch_all(
        """
        SELECT session_id, exam_type, exam_category, employer,
               total_questions, time_limit_seconds, started_at, completed_at,
               score, violations, max_violations, status
        FROM exam_sessions
        WHERE user_id = %s
        ORDER BY started_at DESC
        LIMIT 50
        """,
        (user["id"],)
    )


@router.get("/progress")
def get_user_progress(user: dict = Depends(get_current_user)):
    """Return performance measured only from questions the user has answered."""
    summary = fetch_one(
        """
        SELECT COUNT(DISTINCT es.session_id) AS sessions_started,
               COUNT(DISTINCT es.session_id) FILTER (WHERE es.status IN ('completed', 'cancelled')) AS sessions_completed,
               COUNT(ea.id) FILTER (WHERE ea.selected_answer IS NOT NULL) AS questions_answered,
               COUNT(ea.id) FILTER (
                   WHERE ea.selected_answer IS NOT NULL
                   AND ea.selected_answer = ea.correct_answer
               ) AS correct_answers,
               COALESCE(
                   ROUND(
                       AVG(ea.time_spent_ms) FILTER (
                           WHERE ea.selected_answer IS NOT NULL AND ea.time_spent_ms > 0
                       ) / 1000.0,
                       1
                   ),
                   0
               ) AS average_response_seconds
        FROM exam_sessions es
        LEFT JOIN exam_answers ea ON ea.session_id = es.session_id
        WHERE es.user_id = %s
        """,
        (user["id"],)
    )
    category_rows = fetch_all(
        """
        SELECT COALESCE(NULLIF(q.exam_category, ''), NULLIF(es.exam_category, ''), 'Uncategorised') AS category,
               COUNT(ea.id) FILTER (WHERE ea.selected_answer IS NOT NULL) AS questions_answered,
               COUNT(ea.id) FILTER (
                   WHERE ea.selected_answer IS NOT NULL
                   AND ea.selected_answer = ea.correct_answer
               ) AS correct_answers,
               COUNT(DISTINCT es.session_id) FILTER (WHERE ea.selected_answer IS NOT NULL) AS sessions,
               COALESCE(
                   ROUND(
                       AVG(ea.time_spent_ms) FILTER (
                           WHERE ea.selected_answer IS NOT NULL AND ea.time_spent_ms > 0
                       ) / 1000.0,
                       1
                   ),
                   0
               ) AS average_response_seconds
        FROM exam_sessions es
        JOIN exam_answers ea ON ea.session_id = es.session_id
        LEFT JOIN questions q ON q.question_id = ea.question_id
        WHERE es.user_id = %s
        GROUP BY COALESCE(NULLIF(q.exam_category, ''), NULLIF(es.exam_category, ''), 'Uncategorised')
        HAVING COUNT(ea.id) FILTER (WHERE ea.selected_answer IS NOT NULL) > 0
        """,
        (user["id"],)
    )
    available_categories = fetch_all(
        """
        SELECT exam_category AS category, COUNT(*) AS question_count
        FROM questions
        WHERE exam_category IS NOT NULL AND exam_category <> ''
        GROUP BY exam_category
        ORDER BY exam_category
        """
    )
    recent_sessions = fetch_all(
        """
        SELECT es.session_id, es.exam_type, es.exam_category, es.employer,
               es.total_questions, es.started_at, es.completed_at, es.status,
               COUNT(ea.id) FILTER (WHERE ea.selected_answer IS NOT NULL) AS questions_answered,
               COUNT(ea.id) FILTER (
                   WHERE ea.selected_answer IS NOT NULL
                   AND ea.selected_answer = ea.correct_answer
               ) AS correct_answers
        FROM exam_sessions es
        LEFT JOIN exam_answers ea ON ea.session_id = es.session_id
        WHERE es.user_id = %s
        GROUP BY es.session_id, es.exam_type, es.exam_category, es.employer,
                 es.total_questions, es.started_at, es.completed_at, es.status
        ORDER BY es.started_at DESC
        LIMIT 5
        """,
        (user["id"],)
    )
    weekly_activity = fetch_all(
        """
        WITH weeks AS (
            SELECT generate_series(
                DATE_TRUNC('week', NOW()) - INTERVAL '7 weeks',
                DATE_TRUNC('week', NOW()),
                INTERVAL '1 week'
            )::date AS week_start
        ),
        activity AS (
            SELECT DATE_TRUNC('week', es.started_at)::date AS week_start,
                   COUNT(ea.id) FILTER (WHERE ea.selected_answer IS NOT NULL) AS questions_answered,
                   COUNT(ea.id) FILTER (
                       WHERE ea.selected_answer IS NOT NULL
                       AND ea.selected_answer = ea.correct_answer
                   ) AS correct_answers
            FROM exam_sessions es
            JOIN exam_answers ea ON ea.session_id = es.session_id
            WHERE es.user_id = %s
              AND es.started_at >= DATE_TRUNC('week', NOW()) - INTERVAL '7 weeks'
            GROUP BY DATE_TRUNC('week', es.started_at)::date
        )
        SELECT weeks.week_start,
               COALESCE(activity.questions_answered, 0) AS questions_answered,
               COALESCE(activity.correct_answers, 0) AS correct_answers
        FROM weeks
        LEFT JOIN activity USING (week_start)
        ORDER BY weeks.week_start
        """,
        (user["id"],)
    )

    answered = int(summary["questions_answered"] or 0)
    correct = int(summary["correct_answers"] or 0)
    for row in category_rows:
        row["questions_answered"] = int(row["questions_answered"] or 0)
        row["correct_answers"] = int(row["correct_answers"] or 0)
        row["accuracy"] = round(row["correct_answers"] * 100 / row["questions_answered"], 1)

    if not answered:
        recommendation = {
            "category": "Mixed",
            "reason": "Start with a mixed practice session to build your first performance baseline.",
            "based_on_questions": 0,
            "accuracy": None,
            "model": "thompson_sampling",
        }
    elif available_categories:
        category_stats = {row["category"]: row for row in category_rows}
        bandit_answer_count = sum(
            int(category_stats.get(item["category"], {}).get("questions_answered", 0))
            for item in available_categories
        )
        arms = []
        sampler = random.Random(
            f"{user['id']}:{answered}:" + "|".join(
                f"{row['category']}:{row['questions_answered']}:{row['correct_answers']}"
                for row in sorted(category_rows, key=lambda item: item["category"])
            )
        )
        for item in available_categories:
            stats = category_stats.get(item["category"], {})
            questions_answered = int(stats.get("questions_answered", 0))
            correct_answers = int(stats.get("correct_answers", 0))
            alpha = correct_answers + 1
            beta = questions_answered - correct_answers + 1
            arms.append({
                "category": item["category"],
                "sample": sampler.betavariate(alpha, beta),
                "questions_answered": questions_answered,
                "correct_answers": correct_answers,
                "expected_accuracy": alpha / (alpha + beta),
            })
        selected_arm = max(arms, key=lambda item: item["sample"])
        if selected_arm["questions_answered"]:
            reason = (
                "Your answers train a per-user Thompson-sampling model. "
                "It balances likely progress with trying sections where it is still uncertain."
            )
        else:
            reason = (
                f"The model is exploring {selected_arm['category']} because it has not seen your answers there yet. "
                "Your next answers update this recommendation."
            )
        recommendation = {
            "category": selected_arm["category"],
            "reason": reason,
            "based_on_questions": selected_arm["questions_answered"],
            "accuracy": round(selected_arm["expected_accuracy"] * 100, 1),
            "model": "thompson_sampling",
            "trained_on_answers": bandit_answer_count,
        }
    else:
        recommendation = {
            "category": "Mixed",
            "reason": "No categorized question-bank sections are available yet. Mixed practice is recommended.",
            "based_on_questions": answered,
            "accuracy": None,
            "model": "thompson_sampling",
            "trained_on_answers": 0,
        }

    return {
        "summary": {
            "sessions_started": int(summary["sessions_started"] or 0),
            "sessions_completed": int(summary["sessions_completed"] or 0),
            "questions_answered": answered,
            "correct_answers": correct,
            "accuracy": round(correct * 100 / answered, 1) if answered else 0,
            "average_response_seconds": float(summary["average_response_seconds"] or 0),
        },
        "categories": category_rows,
        "available_categories": available_categories,
        "weekly_activity": weekly_activity,
        "recommendation": recommendation,
        "recent_sessions": recent_sessions,
    }


@router.get("/bookmarks")
def get_bookmarks(user: dict = Depends(get_current_user)):
    """Return all questions the authenticated user has bookmarked."""
    bookmarks = fetch_all(
        """
        SELECT ea.session_id, ea.question_index, ea.question_id, ea.question_text,
               ea.passage, ea.options, ea.correct_answer, ea.correct_answer_text,
               ea.explanation, ea.image_url, es.exam_type, es.exam_category,
               es.started_at, es.status AS session_status
        FROM exam_answers ea
        JOIN exam_sessions es ON es.session_id = ea.session_id
        WHERE es.user_id = %s AND ea.is_flagged = TRUE
        ORDER BY es.started_at DESC, ea.question_index ASC
        """,
        (user["id"],)
    )
    for bookmark in bookmarks:
        bookmark["image_url"] = b2_service.to_proxy_url(bookmark.get("image_url"))
    return bookmarks


@router.get("/streak")
def get_study_streak(user: dict = Depends(get_current_user)):
    """Return the user's current study streak from exam and practice activity."""
    activity_rows = fetch_all(
        """
        SELECT DISTINCT (COALESCE(completed_at, started_at) AT TIME ZONE 'UTC')::date AS activity_date
        FROM exam_sessions
        WHERE user_id = %s
        ORDER BY activity_date DESC
        """,
        (user["id"],)
    )
    activity_dates = [row["activity_date"] for row in activity_rows]
    today = datetime.now(timezone.utc).date()
    return {
        "current_streak": _calculate_current_streak(activity_dates, today),
        "studied_today": today in activity_dates,
    }


@router.get("/{session_id}")
def get_exam_session(session_id: str, user: dict = Depends(get_current_user)):
    """Fetch exam session and questions."""
    session = fetch_one(
        """
        SELECT session_id, user_id, exam_type, exam_category, employer,
               total_questions, time_limit_seconds, started_at, completed_at,
               score, violations, max_violations, status
        FROM exam_sessions
        WHERE session_id = %s AND user_id = %s
        """,
        (session_id, user["id"])
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam session not found")

    answers = fetch_all(
        """
        SELECT ea.question_index, ea.question_id, q.employer, ea.question_text, ea.passage, ea.options,
               ea.selected_answer, ea.is_flagged, ea.time_spent_ms, ea.is_ai_generated,
               ea.correct_answer, ea.correct_answer_text, ea.explanation, ea.is_correct, ea.image_url
        FROM exam_answers ea
        LEFT JOIN questions q ON q.question_id = ea.question_id
        WHERE ea.session_id = %s
        ORDER BY ea.question_index ASC
        """,
        (session_id,)
    )

    is_completed = (session["status"] in ("completed", "cancelled"))
    is_practice = (session["exam_type"] == "practice")

    formatted_answers = []
    for a in answers:
        opts = a.get("options")
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except Exception:
                opts = []
        item = {
            "question_index": a["question_index"],
            "question_id": a["question_id"],
            "employer": a.get("employer"),
            "question_text": a["question_text"],
            "passage": a["passage"],
            "options": opts,
            "selected_answer": a["selected_answer"],
            "is_flagged": a["is_flagged"],
            "time_spent_ms": a["time_spent_ms"],
            "is_ai_generated": a["is_ai_generated"],
            "image_url": b2_service.to_proxy_url(a.get("image_url")),
        }
        if is_completed or is_practice:
            item["correct_answer"] = a["correct_answer"]
            item["correct_answer_text"] = a["correct_answer_text"]
            item["explanation"] = a["explanation"]
            item["is_correct"] = a["is_correct"]
        formatted_answers.append(item)

    return {
        "session": dict(session),
        "questions": formatted_answers,
    }


@router.put("/{session_id}/answer")
def submit_answer(
    session_id: str,
    payload: SubmitAnswerRequest,
    user: dict = Depends(get_current_user),
):
    """Submit or update candidate's chosen answer for a question."""
    session = fetch_one(
        "SELECT id, status FROM exam_sessions WHERE session_id = %s AND user_id = %s",
        (session_id, user["id"])
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam session not found")

    if session["status"] != "active":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Exam is not active")

    answer_row = fetch_one(
        "SELECT correct_answer FROM exam_answers WHERE session_id = %s AND question_index = %s",
        (session_id, payload.question_index)
    )
    if not answer_row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Question index not found")

    is_correct = (payload.selected_answer == answer_row["correct_answer"]) if payload.selected_answer else None

    execute(
        """
        UPDATE exam_answers
        SET selected_answer = %s,
            is_correct = %s,
            time_spent_ms = COALESCE(time_spent_ms, 0) + %s
        WHERE session_id = %s AND question_index = %s
        """,
        (payload.selected_answer, is_correct, payload.time_spent_ms or 0, session_id, payload.question_index)
    )

    return {
        "status": "ok",
        "question_index": payload.question_index,
        "selected_answer": payload.selected_answer,
    }


@router.put("/{session_id}/flag/{question_index}")
def toggle_flag(
    session_id: str,
    question_index: int,
    payload: Optional[FlagRequest] = None,
    user: dict = Depends(get_current_user),
):
    """Toggle or update flag on a question."""
    session = fetch_one(
        "SELECT id FROM exam_sessions WHERE session_id = %s AND user_id = %s",
        (session_id, user["id"])
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam session not found")

    if payload and payload.is_flagged is not None:
        new_flag = payload.is_flagged
    else:
        current = fetch_one(
            "SELECT is_flagged FROM exam_answers WHERE session_id = %s AND question_index = %s",
            (session_id, question_index)
        )
        if not current:
            raise HTTPException(status_code=404, detail="Question not found")
        new_flag = not current["is_flagged"]

    execute(
        "UPDATE exam_answers SET is_flagged = %s WHERE session_id = %s AND question_index = %s",
        (new_flag, session_id, question_index)
    )
    return {"status": "ok", "question_index": question_index, "is_flagged": new_flag}


@router.post("/{session_id}/violation")
def record_violation(
    session_id: str,
    payload: ViolationRequest,
    user: dict = Depends(get_current_user),
):
    """Record a proctoring violation (e.g. fullscreen exit or tab switch)."""
    session = fetch_one(
        "SELECT violations, max_violations, status FROM exam_sessions WHERE session_id = %s AND user_id = %s",
        (session_id, user["id"])
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam session not found")

    new_count = (session["violations"] or 0) + 1
    max_v = session["max_violations"] or 3

    # Record violation
    execute(
        """
        INSERT INTO exam_violations (session_id, violation_type, violation_number)
        VALUES (%s, %s, %s)
        """,
        (session_id, payload.violation_type, new_count)
    )

    cancelled = (new_count >= max_v)
    if cancelled:
        # Auto-cancel exam and tally score
        score_row = fetch_one(
            "SELECT COUNT(*) AS score FROM exam_answers WHERE session_id = %s AND is_correct = TRUE",
            (session_id,)
        )
        final_score = score_row["score"] if score_row else 0
        execute(
            """
            UPDATE exam_sessions
            SET violations = %s,
                status = 'cancelled',
                completed_at = NOW(),
                score = %s
            WHERE session_id = %s
            """,
            (new_count, final_score, session_id)
        )
        current_status = "cancelled"
    else:
        execute(
            "UPDATE exam_sessions SET violations = %s WHERE session_id = %s",
            (new_count, session_id)
        )
        current_status = session["status"]

    return {
        "violations": new_count,
        "max_violations": max_v,
        "status": current_status,
        "cancelled": cancelled,
    }


@router.post("/{session_id}/complete")
def complete_exam(session_id: str, user: dict = Depends(get_current_user)):
    """Complete the exam and calculate total score."""
    session = fetch_one(
        "SELECT total_questions, status FROM exam_sessions WHERE session_id = %s AND user_id = %s",
        (session_id, user["id"])
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam session not found")

    score_row = fetch_one(
        "SELECT COUNT(*) AS score FROM exam_answers WHERE session_id = %s AND is_correct = TRUE",
        (session_id,)
    )
    score = score_row["score"] if score_row else 0
    total = session["total_questions"] or 1

    execute(
        """
        UPDATE exam_sessions
        SET status = 'completed',
            completed_at = NOW(),
            score = %s
        WHERE session_id = %s
        """,
        (score, session_id)
    )

    return {
        "status": "completed",
        "score": score,
        "total_questions": total,
        "percentage": round((score / total) * 100, 1),
    }


@router.get("/{session_id}/results")
def get_exam_results(session_id: str, user: dict = Depends(get_current_user)):
    """Return in-depth score analysis, performance metrics, and question review."""
    session = fetch_one(
        """
        SELECT session_id, user_id, exam_type, exam_category, employer,
               total_questions, time_limit_seconds, started_at, completed_at,
               score, violations, max_violations, status
        FROM exam_sessions
        WHERE session_id = %s AND user_id = %s
        """,
        (session_id, user["id"])
    )
    if not session:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Exam session not found")

    answers = fetch_all(
        """
        SELECT ea.question_index, ea.question_id, q.employer, ea.question_text, ea.passage, ea.options,
               ea.selected_answer, ea.correct_answer, ea.correct_answer_text,
               ea.explanation, ea.is_correct, ea.is_flagged, ea.time_spent_ms, ea.is_ai_generated, ea.image_url
        FROM exam_answers ea
        LEFT JOIN questions q ON q.question_id = ea.question_id
        WHERE ea.session_id = %s
        ORDER BY ea.question_index ASC
        """,
        (session_id,)
    )

    # Compute stats
    total = session["total_questions"] or len(answers)
    answered_count = sum(1 for a in answers if a["selected_answer"])
    correct_count = sum(1 for a in answers if a["is_correct"] is True)
    incorrect_count = sum(1 for a in answers if a["selected_answer"] and a["is_correct"] is False)
    unanswered_count = total - answered_count
    flagged_count = sum(1 for a in answers if a["is_flagged"])

    total_time_ms = sum(a["time_spent_ms"] or 0 for a in answers)
    avg_time_per_q = round(total_time_ms / total / 1000, 1) if total > 0 else 0

    percentage = round((correct_count / total) * 100, 1) if total > 0 else 0

    formatted_answers = []
    for a in answers:
        opts = a.get("options")
        if isinstance(opts, str):
            try:
                opts = json.loads(opts)
            except Exception:
                opts = []
        formatted_answers.append({
            "question_index": a["question_index"],
            "question_id": a["question_id"],
            "employer": a.get("employer"),
            "question_text": a["question_text"],
            "passage": a["passage"],
            "options": opts,
            "selected_answer": a["selected_answer"],
            "correct_answer": a["correct_answer"],
            "correct_answer_text": a["correct_answer_text"],
            "explanation": a["explanation"],
            "is_correct": a["is_correct"],
            "is_flagged": a["is_flagged"],
            "time_spent_ms": a["time_spent_ms"],
            "is_ai_generated": a["is_ai_generated"],
            "image_url": b2_service.to_proxy_url(a.get("image_url")),
        })

    return {
        "session": dict(session),
        "analytics": {
            "score": correct_count,
            "total_questions": total,
            "percentage": percentage,
            "answered_count": answered_count,
            "correct_count": correct_count,
            "incorrect_count": incorrect_count,
            "unanswered_count": unanswered_count,
            "flagged_count": flagged_count,
            "avg_time_per_q_seconds": avg_time_per_q,
            "violations": session["violations"],
            "max_violations": session["max_violations"],
        },
        "questions": formatted_answers,
    }
