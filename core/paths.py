"""Project-root paths. Every module should import from here instead of walking __file__."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

CORE = ROOT / "core"
INTEGRATIONS = ROOT / "integrations"
UI = ROOT / "ui"
SKILLS = ROOT / "skills"
TOOLS = ROOT / "tools"
CONFIG = ROOT / "config"
CREDENTIALS = ROOT / "credentials"
DATA = ROOT / "data"
LOGS = DATA / "logs"
MODELS = DATA / "models"
PODCASTS = DATA / "podcasts"
ASSETS = ROOT / "assets"
TESTS = ROOT / "tests"
