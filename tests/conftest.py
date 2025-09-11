import os
import sys


# Ensure the project root is importable (so `import src...` works in tests)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

# Ensure a dummy Dealpath API key is present for module import during tests
os.environ.setdefault("dealpath_key", "test")
