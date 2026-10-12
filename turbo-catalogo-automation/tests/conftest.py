import os
import sys
import tempfile
from pathlib import Path

# Estado aislado y modo offline ANTES de importar el paquete.
_TMP = Path(tempfile.mkdtemp(prefix="turbo_tests_"))
os.environ.update({
    "TURBO_STATE_DIR": str(_TMP / "state"),
    "TURBO_REPORTS_DIR": str(_TMP / "reports"),
    "TURBO_LOGS_DIR": str(_TMP / "logs"),
    "EMBEDDINGS": "hash",
    "MODO_OFFLINE": "1",
    "DRY_RUN": "1",
    "WEBHOOK_SECRET": "secreto-de-test",
})
for k in ("SNOWFLAKE_ACCOUNT", "SIMULAR_FALLA_FUENTE", "SLACK_WEBHOOK_URL", "LLAMA_CLOUD_API_KEY"):
    os.environ.pop(k, None)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
