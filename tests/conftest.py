from typing import Any

import pytest


@pytest.fixture
def full_payment() -> dict[str, Any]:
    return {
        "item": "boAt Airdopes 141",
        "quantity": 1,
        "price": "Rs 1,299",
        "address": "Home",
        "payment_method": "saved HDFC card",
    }
