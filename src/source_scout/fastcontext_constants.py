PROMPT_VERSION = "code-investigation-v5"
SCHEMA_VERSION = "code-investigation-evidence-v3"
ANALYZER_VERSION = "code-investigation-harness-v4"

DEFAULT_MAX_TURNS = 7
MAX_TOOL_CALLS_PER_TURN = 5
MAX_GLOB_RESULTS = 80
MAX_GREP_RESULTS = 80
MAX_READ_LINES = 160
MAX_READ_FILE_BYTES = 240_000
MAX_GREP_FILE_BYTES = 1_000_000
MAX_CITATION_LINES = 240
MAX_FINAL_CITATION_CHOICES = 24
MAX_FINAL_CITATIONS = 3
MAX_FALLBACK_CITATIONS = 3
MAX_FINAL_FILES = 3
TARGET_FINAL_CITATIONS = 2
FOCUSED_FINAL_CITATION_LINES = 80
PRIORITY_OBSERVATION_PATH_LIMIT = 4
RG_TIMEOUT_SECONDS = 10
LOCAL_CONTEXT_FILE_LIMIT = 40
LOCAL_CONTEXT_GREP_LIMIT = 20
LOCAL_EXTRA_SKIP_DIRS = {".agents", ".next", ".source_scout", "build", "coverage", "dist"}
LOCAL_SKIP_FILE_NAMES = {
    "package-lock.json",
    "pnpm-lock.yaml",
    "tsconfig.tsbuildinfo",
    "yarn.lock",
}
PRIMARY_SOURCE_PREFIXES = ("src/source_scout/", "src/", "app/", "components/", "lib/")
NOISY_EVIDENCE_PREFIXES = (
    ".agents/",
    "docs/",
    "eval/",
    "evals/",
    "scripts/",
    "supabase/migrations/",
    "tests/",
)
NOISY_EVIDENCE_FILES = {"README.md", "AGENTS.md", "pyproject.toml"}
LOCAL_TASK_STOPWORDS = {
    "actual",
    "and",
    "are",
    "as",
    "before",
    "be",
    "code",
    "find",
    "for",
    "from",
    "into",
    "is",
    "of",
    "or",
    "local",
    "registered",
    "repo",
    "task",
    "that",
    "the",
    "this",
    "to",
    "where",
    "with",
    "working",
}
