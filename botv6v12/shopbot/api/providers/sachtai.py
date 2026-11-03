"""Sachtai Dealer API contract constants.

Runtime integration lives in bot.py to preserve the legacy single-file bot state,
but these constants define the provider boundary and document the only Sachtai
endpoints used by the application.
"""

PROVIDER = "SACHTAI"
DEFAULT_BASE_URL = "https://bot.sachtai.com"
CAPCUT_ADD_TEAM_PRODUCT_KEY = "CAPCUT_PRO_30D_ADD_TEAM_API"

STOCK_ENDPOINT = "/api/dealer/stock"
BALANCE_ENDPOINT = "/api/dealer/balance"
BUY_ENDPOINT = "/api/dealer/buy"
ORDER_STATUS_ENDPOINT_TEMPLATE = "/api/dealer/orders/{code}"

ALLOWED_ENDPOINTS = frozenset(
    {
        STOCK_ENDPOINT,
        BALANCE_ENDPOINT,
        BUY_ENDPOINT,
        ORDER_STATUS_ENDPOINT_TEMPLATE,
    }
)
