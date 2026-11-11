import os
from dotenv import load_dotenv

# Load .env from project root
load_dotenv(os.path.join(os.path.dirname(__file__), '..', '.env'))

DATABASE_URL = os.getenv('DATABASE_URL', '')
GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '')
GOOGLE_CLIENT_SECRET = os.getenv('GOOGLE_CLIENT_SECRET', '')
JWT_SECRET = os.getenv('JWT_SECRET', 'change-me-in-production')
FRONTEND_URL = os.getenv('FRONTEND_URL', 'http://localhost:5173')
PORT = int(os.getenv('PORT', '8000'))

# Backblaze B2 Storage
B2_APP_KEY = os.getenv('b2_app_key') or os.getenv('B2_APP_KEY', '')
B2_KEY_ID = os.getenv('b2_key_id') or os.getenv('B2_KEY_ID', '')
B2_BUCKET_NAME = os.getenv('B2_BUCKET_NAME', 'gt-study-pack')

# Gemini model to use
GEMINI_MODEL = 'gemini-2.0-flash'
GEMINI_API_BASE = 'https://generativelanguage.googleapis.com/v1beta'
