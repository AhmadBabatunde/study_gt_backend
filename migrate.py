"""
Database migration script.
Creates all required tables for the Study GT application.
Run: python migrate.py
"""

import psycopg2
import sys
import os

sys.path.insert(0, os.path.dirname(__file__))
from config import DATABASE_URL


MIGRATIONS = [
    # Ensure the questions table exists (should already be there)
    """
    CREATE TABLE IF NOT EXISTS questions (
        question_id VARCHAR(100) PRIMARY KEY,
        employer VARCHAR(100),
        exam_category VARCHAR(100),
        question_type VARCHAR(255),
        test_no INTEGER,
        q_no INTEGER,
        passage TEXT,
        question TEXT NOT NULL,
        options JSONB,
        correct_answer VARCHAR(50),
        correct_answer_text TEXT,
        answer_source VARCHAR(100),
        explanation TEXT,
        difficulty VARCHAR(50),
        source TEXT,
        year INTEGER,
        image_url TEXT,
        local_image_path TEXT,
        source_issue TEXT,
        needs_review BOOLEAN DEFAULT FALSE,
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );
    """,

    # Users table (Google OAuth accounts)
    """
    CREATE TABLE IF NOT EXISTS users (
        id SERIAL PRIMARY KEY,
        google_id VARCHAR(255) UNIQUE NOT NULL,
        email VARCHAR(255) UNIQUE NOT NULL,
        name VARCHAR(255),
        picture_url TEXT,
        access_token TEXT,
        refresh_token TEXT,
        token_expiry TIMESTAMPTZ,
        created_at TIMESTAMPTZ DEFAULT NOW(),
        last_login TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Exam sessions
    """
    CREATE TABLE IF NOT EXISTS exam_sessions (
        id SERIAL PRIMARY KEY,
        session_id UUID DEFAULT gen_random_uuid() UNIQUE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        exam_type VARCHAR(50) NOT NULL,
        exam_category VARCHAR(100),
        employer VARCHAR(100) DEFAULT 'Dragnet',
        total_questions INTEGER NOT NULL,
        time_limit_seconds INTEGER NOT NULL,
        started_at TIMESTAMPTZ DEFAULT NOW(),
        completed_at TIMESTAMPTZ,
        score INTEGER DEFAULT 0,
        violations INTEGER DEFAULT 0,
        max_violations INTEGER DEFAULT 3,
        status VARCHAR(20) DEFAULT 'active',
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Exam answers
    """
    CREATE TABLE IF NOT EXISTS exam_answers (
        id SERIAL PRIMARY KEY,
        session_id UUID REFERENCES exam_sessions(session_id) ON DELETE CASCADE,
        question_index INTEGER NOT NULL,
        question_id VARCHAR(100),
        question_text TEXT NOT NULL,
        passage TEXT,
        options JSONB NOT NULL,
        selected_answer VARCHAR(50),
        correct_answer VARCHAR(50) NOT NULL,
        correct_answer_text TEXT,
        explanation TEXT,
        is_correct BOOLEAN,
        is_ai_generated BOOLEAN DEFAULT FALSE,
        is_flagged BOOLEAN DEFAULT FALSE,
        image_url TEXT,
        time_spent_ms INTEGER DEFAULT 0,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Add image_url to exam_answers if table was already created
    "ALTER TABLE exam_answers ADD COLUMN IF NOT EXISTS image_url TEXT;",

    # Exam violations
    """
    CREATE TABLE IF NOT EXISTS exam_violations (
        id SERIAL PRIMARY KEY,
        session_id UUID REFERENCES exam_sessions(session_id) ON DELETE CASCADE,
        violation_type VARCHAR(50) NOT NULL,
        violation_number INTEGER NOT NULL,
        created_at TIMESTAMPTZ DEFAULT NOW()
    );
    """,

    # Community feedback posts and user votes
    """
    CREATE TABLE IF NOT EXISTS feedback_posts (
        id BIGSERIAL PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        category VARCHAR(32) NOT NULL CHECK (category IN ('app', 'test', 'employer', 'administrator')),
        topic VARCHAR(120) NOT NULL,
        body TEXT NOT NULL CHECK (char_length(body) BETWEEN 1 AND 2000),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """,
    """
    CREATE TABLE IF NOT EXISTS feedback_votes (
        post_id BIGINT NOT NULL REFERENCES feedback_posts(id) ON DELETE CASCADE,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        vote SMALLINT NOT NULL CHECK (vote IN (-1, 1)),
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (post_id, user_id)
    );
    """,

    # Indexes for performance
    "CREATE INDEX IF NOT EXISTS idx_questions_category ON questions(exam_category);",
    "CREATE INDEX IF NOT EXISTS idx_questions_employer ON questions(employer);",
    "CREATE INDEX IF NOT EXISTS idx_exam_sessions_user ON exam_sessions(user_id);",
    "CREATE INDEX IF NOT EXISTS idx_exam_sessions_user_started ON exam_sessions(user_id, started_at DESC);",
    "CREATE INDEX IF NOT EXISTS idx_exam_sessions_status ON exam_sessions(status);",
    "CREATE INDEX IF NOT EXISTS idx_exam_answers_session ON exam_answers(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_exam_violations_session ON exam_violations(session_id);",
    "CREATE INDEX IF NOT EXISTS idx_users_google_id ON users(google_id);",
    "CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);",
    "CREATE INDEX IF NOT EXISTS idx_feedback_posts_created ON feedback_posts(created_at DESC, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_feedback_posts_category_created ON feedback_posts(category, created_at DESC, id DESC);",
    "CREATE INDEX IF NOT EXISTS idx_feedback_votes_user ON feedback_votes(user_id, post_id);",
]


def run_migrations():
    """Execute all migration statements."""
    print("Connecting to database...")
    conn = psycopg2.connect(DATABASE_URL)

    try:
        with conn.cursor() as cur:
            for i, sql in enumerate(MIGRATIONS):
                label = sql.strip().split('\n')[0].strip()[:60]
                print(f"  [{i+1}/{len(MIGRATIONS)}] {label}...")
                cur.execute(sql)

        conn.commit()
        print(f"\n[OK] All {len(MIGRATIONS)} migrations completed successfully.")

        # Print table summary
        with conn.cursor() as cur:
            cur.execute("""
                SELECT table_name, 
                       (SELECT COUNT(*) FROM information_schema.columns c 
                        WHERE c.table_name = t.table_name AND c.table_schema = 'public') as col_count
                FROM information_schema.tables t
                WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
                ORDER BY table_name;
            """)
            tables = cur.fetchall()
            print("\nDatabase tables:")
            for name, cols in tables:
                cur.execute(f"SELECT COUNT(*) FROM {name}")
                rows = cur.fetchone()[0]
                print(f"  * {name}: {cols} columns, {rows} rows")

    except Exception as e:
        conn.rollback()
        print(f"\n[FAIL] Migration failed: {e}")
        raise
    finally:
        conn.close()


if __name__ == '__main__':
    run_migrations()
