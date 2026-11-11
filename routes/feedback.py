"""Community feedback posts and usefulness voting."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator

from auth import get_current_user
from db import execute, execute_returning, fetch_all, fetch_one

router = APIRouter(prefix="/api/feedback", tags=["feedback"])

FeedbackCategory = Literal["app", "test", "employer", "administrator"]
GENERAL_ASSESSMENT_TOPIC = "General assessment tips"


class CreateFeedbackRequest(BaseModel):
    category: FeedbackCategory
    topic: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=2000)

    @field_validator("topic", "body")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("This field cannot be blank.")
        return value


class FeedbackVoteRequest(BaseModel):
    vote: int = Field(ge=-1, le=1)

    @field_validator("vote")
    @classmethod
    def validate_vote(cls, value: int) -> int:
        if value not in (-1, 0, 1):
            raise ValueError("Vote must be -1, 0, or 1.")
        return value


def _feedback_post_for_user(post_id: int, user_id: int) -> Optional[dict]:
    return fetch_one(
        """
        SELECT p.id, p.user_id, u.name AS author_name, p.category, p.topic,
               p.body, p.created_at,
               COUNT(v.user_id) FILTER (WHERE v.vote = 1)::int AS upvotes,
               COUNT(v.user_id) FILTER (WHERE v.vote = -1)::int AS downvotes,
               COALESCE(SUM(v.vote), 0)::int AS score,
               COALESCE(MAX(own_vote.vote), 0)::int AS user_vote
        FROM feedback_posts p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN feedback_votes v ON v.post_id = p.id
        LEFT JOIN feedback_votes own_vote
          ON own_vote.post_id = p.id AND own_vote.user_id = %s
        WHERE p.id = %s
        GROUP BY p.id, u.name
        """,
        (user_id, post_id),
    )


@router.get("")
def list_feedback(
    category: Optional[FeedbackCategory] = None,
    sort: Literal["top", "newest"] = "top",
    limit: int = Query(default=20, ge=1, le=50),
    offset: int = Query(default=0, ge=0),
    user: dict = Depends(get_current_user),
):
    """List feedback ordered by vote score or recency."""
    ordering = (
        "score DESC, p.created_at DESC, p.id DESC"
        if sort == "top"
        else "p.created_at DESC, p.id DESC"
    )
    rows = fetch_all(
        f"""
        WITH vote_totals AS (
            SELECT post_id,
                   COUNT(*) FILTER (WHERE vote = 1)::int AS upvotes,
                   COUNT(*) FILTER (WHERE vote = -1)::int AS downvotes,
                   SUM(vote)::int AS score
            FROM feedback_votes
            GROUP BY post_id
        )
        SELECT p.id, p.user_id, u.name AS author_name, p.category, p.topic,
               p.body, p.created_at,
               COALESCE(vt.upvotes, 0)::int AS upvotes,
               COALESCE(vt.downvotes, 0)::int AS downvotes,
               COALESCE(vt.score, 0)::int AS score,
               COALESCE(own_vote.vote, 0)::int AS user_vote,
               COUNT(*) OVER()::int AS total_count
        FROM feedback_posts p
        JOIN users u ON u.id = p.user_id
        LEFT JOIN vote_totals vt ON vt.post_id = p.id
        LEFT JOIN feedback_votes own_vote
          ON own_vote.post_id = p.id AND own_vote.user_id = %s
        WHERE (%s IS NULL OR p.category = %s)
        ORDER BY {ordering}
        LIMIT %s OFFSET %s
        """,
        (user["id"], category, category, limit, offset),
    )
    total = int(rows[0]["total_count"]) if rows else 0
    posts = []
    for row in rows:
        post = dict(row)
        post.pop("total_count", None)
        posts.append(post)
    return {"posts": posts, "total": total, "limit": limit, "offset": offset}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_feedback(
    request: CreateFeedbackRequest,
    user: dict = Depends(get_current_user),
):
    """Create a feedback post owned by the authenticated user."""
    if request.category == "test" and request.topic != GENERAL_ASSESSMENT_TOPIC:
        topic_exists = fetch_one(
            "SELECT EXISTS(SELECT 1 FROM questions WHERE exam_category = %s) AS found",
            (request.topic,),
        )["found"]
        if not topic_exists:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Choose an assessment section from the question bank.",
            )
    elif request.category == "employer":
        topic_exists = fetch_one(
            "SELECT EXISTS(SELECT 1 FROM questions WHERE employer = %s) AS found",
            (request.topic,),
        )["found"]
        if not topic_exists:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Choose an employer from the question bank.",
            )

    row = execute_returning(
        """
        INSERT INTO feedback_posts (user_id, category, topic, body)
        VALUES (%s, %s, %s, %s)
        RETURNING id
        """,
        (user["id"], request.category, request.topic, request.body),
    )
    post = _feedback_post_for_user(int(row["id"]), user["id"])
    if not post:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Feedback was created but could not be loaded.",
        )
    return post


@router.put("/{post_id}/vote")
def vote_feedback(
    post_id: int,
    request: FeedbackVoteRequest,
    user: dict = Depends(get_current_user),
):
    """Set, change, or remove the current user's vote on a post."""
    post = fetch_one(
        "SELECT id, user_id FROM feedback_posts WHERE id = %s",
        (post_id,),
    )
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found.")
    if int(post["user_id"]) == int(user["id"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You cannot vote on your own feedback.")

    if request.vote == 0:
        execute(
            "DELETE FROM feedback_votes WHERE post_id = %s AND user_id = %s",
            (post_id, user["id"]),
        )
    else:
        execute(
            """
            INSERT INTO feedback_votes (post_id, user_id, vote)
            VALUES (%s, %s, %s)
            ON CONFLICT (post_id, user_id)
            DO UPDATE SET vote = EXCLUDED.vote, created_at = NOW()
            """,
            (post_id, user["id"], request.vote),
        )

    post = _feedback_post_for_user(post_id, user["id"])
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found.")
    return post


@router.delete("/{post_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_feedback(
    post_id: int,
    user: dict = Depends(get_current_user),
):
    """Delete a post only when it belongs to the authenticated user."""
    deleted = execute_returning(
        """
        DELETE FROM feedback_posts
        WHERE id = %s AND user_id = %s
        RETURNING id
        """,
        (post_id, user["id"]),
    )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback not found.")
