"""
GUITAR ATLAS TH-07a Stripe plan helpers.

Created: 2026-05-18
Purpose: Resolve Premium monthly/yearly plan Price IDs from environment names only.
"""
from __future__ import annotations

import os
from typing import Literal

PlanName = Literal["monthly", "yearly"]


def resolve_price_id(plan: PlanName) -> str:
    """Return the Stripe Price ID for the requested Premium plan.

    Args:
        plan: "monthly" or "yearly".

    Returns:
        The Price ID stored in the corresponding environment variable.

    Raises:
        ValueError: If plan is not supported.
        KeyError: If the required environment variable is unset.
    """
    price_env = {
        "monthly": "STRIPE_PRICE_ID_MONTHLY",
        "yearly": "STRIPE_PRICE_ID_YEARLY",
    }
    if plan not in price_env:
        raise ValueError(f"unsupported plan: {plan}")
    return os.environ[price_env[plan]]
