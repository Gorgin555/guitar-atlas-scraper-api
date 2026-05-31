"""
GUITAR ATLAS — Index Engine Package
=====================================
GAI-E / MFI / VFI / BPI 指標算出エンジン

起動:
    cd ~/Desktop/ATLAS/code
    source .venv/bin/activate
    python -m index_engine.run_test        # CEO向けテストラン
    python -m index_engine.engine          # 日次本番実行

設計者: CSO スラリン
実装日: 2026-05-14
"""
from .models import (
    ComponentBreakdown,
    ModelScore,
    IndexResult,
    SpreadResult,
    DailyIndexReport,
    COMPONENT_WEIGHTS,
    GAI_E_WEIGHTS,
    SPREAD_DEFINITIONS,
    VFI_CONDITION_TAGS,
)

__version__ = "1.0.0"
__all__ = [
    "ComponentBreakdown",
    "ModelScore",
    "IndexResult",
    "SpreadResult",
    "DailyIndexReport",
    "COMPONENT_WEIGHTS",
    "GAI_E_WEIGHTS",
    "SPREAD_DEFINITIONS",
    "VFI_CONDITION_TAGS",
]
