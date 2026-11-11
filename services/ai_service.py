"""
AI Service for Study GT.
Uses Gemini API with the user's Google OAuth access token (or admin API key as fallback)
to generate high-standard aptitude test questions mirroring the Dragnet style.
"""

import json
import logging
import re
from typing import List, Dict, Any, Optional
import httpx
from config import GEMINI_MODEL, GEMINI_API_BASE, GEMINI_API_KEY

logger = logging.getLogger(__name__)


def _format_sample_questions(samples: List[Dict[str, Any]]) -> str:
    """Format sample questions from the database as prompt reference."""
    if not samples:
        return "No specific samples provided. Generate standard psychometric/aptitude questions."

    formatted = []
    for i, s in enumerate(samples[:4], start=1):
        q_type = s.get("question_type") or "Multiple Choice"
        cat = s.get("exam_category") or "General"
        passage = f"\nPassage: {s['passage']}" if s.get("passage") else ""
        options_str = ""
        options = s.get("options")
        if isinstance(options, str):
            try:
                options = json.loads(options)
            except Exception:
                options = []
        if isinstance(options, list):
            options_str = "\n" + "\n".join([f"  {opt.get('key')}: {opt.get('text')}" for opt in options])

        correct = s.get("correct_answer", "")
        correct_text = s.get("correct_answer_text", "")
        explanation = s.get("explanation", "")

        formatted.append(
            f"Sample {i} ({cat} - {q_type}):{passage}\n"
            f"Question: {s.get('question')}{options_str}\n"
            f"Correct: {correct} ({correct_text})\n"
            f"Explanation: {explanation}\n"
        )
    return "\n---\n".join(formatted)


def _clean_json_response(raw_text: str) -> List[Dict[str, Any]]:
    """Clean markdown code fences or extraneous text and parse JSON array."""
    cleaned = raw_text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    cleaned = cleaned.strip()

    # Sometimes model prepends or appends notes, search for the outer JSON array [...]
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(0)

    parsed = json.loads(cleaned)
    if not isinstance(parsed, list):
        raise ValueError("Parsed JSON is not a list")
    return parsed


async def generate_questions(
    user_access_token: Optional[str],
    category: str,
    count: int = 5,
    sample_questions: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Generate aptitude test questions using Gemini API.
    Attempts using user's Google OAuth token first.
    If unavailable or invalid, falls back to GEMINI_API_KEY if configured.
    """
    sample_text = _format_sample_questions(sample_questions or [])

    system_instruction = (
        "You are an expert psychometric assessment developer creating rigorous graduate trainee "
        "aptitude test questions for Dragnet Nigeria / GTCO / Tier-1 investment bank style exams. "
        "Every question must have clear logic, 4 or 5 unambiguous options (A, B, C, D, E or True/False/Cannot Say for Verbal), "
        "a precisely marked correct answer key, and an insightful, educational explanation."
    )

    prompt = f"""Generate exactly {count} {category} aptitude test questions.

Reference the tone, structure, and difficulty of these Dragnet past questions:
{sample_text}

Requirements for {category}:
- For Verbal Reasoning: You may include an insightful passage (business, science, economics, or general logic) and ask Reading Comprehension (True/False/Cannot say or Multiple Choice) or Sentence Correction / Critical Reasoning questions.
- For Numerical Reasoning: Data interpretation, percentages, currency, ratios, speed-distance-time, or logical numerical sequences. Make sure numerical facts and calculations are 100% accurate.
- For Abstract/Diagrammatic: Detailed verbal descriptions of matrix rules, rotation/reflection symmetries, element count changes, or pattern deduction questions with symbolic answer options.

Output MUST be a valid JSON array of objects with the exact schema:
[
  {{
    "question": "The question text here",
    "passage": "Passage text if applicable, otherwise null",
    "options": [
      {{"key": "A", "text": "Option text A"}},
      {{"key": "B", "text": "Option text B"}},
      {{"key": "C", "text": "Option text C"}},
      {{"key": "D", "text": "Option text D"}}
    ],
    "correct_answer": "A",
    "correct_answer_text": "Option text A",
    "explanation": "Detailed explanation of why this answer is correct and others are incorrect.",
    "difficulty": "Medium",
    "question_type": "Multiple Choice"
  }}
]
Return ONLY the JSON array, with no other text, markdown formatting or commentary.
"""

    headers = {"Content-Type": "application/json"}
    url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent"

    # Decide authentication: user OAuth token or admin API key
    if user_access_token:
        headers["Authorization"] = f"Bearer {user_access_token}"
    elif GEMINI_API_KEY:
        url = f"{url}?key={GEMINI_API_KEY}"
    else:
        raise ValueError("Neither user Google OAuth token nor GEMINI_API_KEY is available for AI generation.")

    payload = {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": f"{system_instruction}\n\n{prompt}"}]
            }
        ],
        "generationConfig": {
            "temperature": 0.4,
            "responseMimeType": "application/json"
        }
    }

    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(url, headers=headers, json=payload)

        # If user OAuth token failed (e.g. scope missing or expired) and we have admin API key fallback:
        if response.status_code != 200 and user_access_token and GEMINI_API_KEY:
            logger.warning(
                f"User OAuth Gemini call failed with {response.status_code}: {response.text}. Retrying with GEMINI_API_KEY."
            )
            fallback_url = f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
            fallback_headers = {"Content-Type": "application/json"}
            response = await client.post(fallback_url, headers=fallback_headers, json=payload)

        if response.status_code != 200:
            raise RuntimeError(f"Gemini API returned error {response.status_code}: {response.text}")

        res_data = response.json()
        candidates = res_data.get("candidates", [])
        if not candidates:
            raise RuntimeError("Gemini returned empty candidates")

        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            raise RuntimeError("Gemini returned no content parts")

        raw_text = parts[0].get("text", "")
        questions = _clean_json_response(raw_text)

        # Sanitize items
        sanitized = []
        for q in questions:
            opts = q.get("options", [])
            # Format options list if needed
            if isinstance(opts, dict):
                opts = [{"key": k, "text": v} for k, v in opts.items()]
            sanitized.append({
                "question": q.get("question", "Untitled Question"),
                "passage": q.get("passage"),
                "options": opts,
                "correct_answer": q.get("correct_answer", "A"),
                "correct_answer_text": q.get("correct_answer_text", ""),
                "explanation": q.get("explanation", "No explanation available."),
                "difficulty": q.get("difficulty", "Medium"),
                "question_type": q.get("question_type", "Multiple Choice"),
                "is_ai_generated": True,
            })

        return sanitized

