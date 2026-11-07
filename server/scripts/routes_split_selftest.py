"""Regression guard for the routes.py split.

Ensures the per-domain router split preserved EVERY endpoint: the set of
(method, path) exposed by ``app.api.routes.router`` must equal the baseline
captured when routes.py was a single file.

Run: .venv/Scripts/python.exe server/scripts/routes_split_selftest.py
"""
import os
import sys

# Make the ``app`` package importable when run as a standalone script.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.api import routes as R

EXPECTED = [('GET', '/api/health'), ('GET', '/api/data-sources/health'), ('POST', '/api/data-sources/eastmoney-pause'), ('POST', '/api/data-sources/eastmoney-resume'), ('GET', '/api/hot_search_ranking'), ('GET', '/api/tab-scan-config'), ('GET', '/api/api-usage'), ('POST', '/api/api-usage/limits'), ('GET', '/api/eval-lab/latest'), ('POST', '/api/eval-lab/run'), ('GET', '/api/auction/latest'), ('GET', '/api/update-hot_search_ranking'), ('GET', '/api/stock/{symbol}'), ('GET', '/api/stock/{symbol}/analysis'), ('GET', '/api/stock/{symbol}/full'), ('GET', '/api/stocks'), ('GET', '/api/daily_recommendations'), ('GET', '/api/recommend-review'), ('GET', '/api/recommend-sim'), ('GET', '/api/recommend-batches'), ('GET', '/api/recommend-sim-batch'), ('GET', '/api/recommend-excess'), ('GET', '/api/shadow-eval'), ('GET', '/api/stock-search'), ('POST', '/api/auth/register'), ('POST', '/api/auth/login'), ('POST', '/api/auth/logout'), ('GET', '/api/auth/me'), ('GET', '/api/auth/users'), ('GET', '/api/auth/status'), ('POST', '/api/auth/change-password'), ('GET', '/api/positions'), ('POST', '/api/positions'), ('POST', '/api/positions/capital'), ('GET', '/api/push-channels'), ('POST', '/api/push-channels'), ('GET', '/api/push-channels/discover'), ('POST', '/api/push-channels/search-user'), ('POST', '/api/push-channels/aibot'), ('GET', '/api/push-channels/agent/status'), ('POST', '/api/push-channels/agent'), ('POST', '/api/push-channels/{ch_id}/test'), ('DELETE', '/api/push-channels/{ch_id}'), ('POST', '/api/positions/adjust'), ('DELETE', '/api/positions/{symbol}'), ('GET', '/api/sim/state'), ('POST', '/api/sim/generate'), ('POST', '/api/sim/execute'), ('POST', '/api/sim/sell'), ('POST', '/api/sim/reset'), ('POST', '/api/daily_recommendations'), ('DELETE', '/api/daily_recommendations/{date}'), ('POST', '/api/generate-recommendations'), ('POST', '/api/scan-ma10'), ('GET', '/api/trend-stocks'), ('GET', '/api/trend/history-counts'), ('GET', '/api/scan-trend-stocks'), ('POST', '/api/trend/backtest'), ('GET', '/api/trend/backtest-history'), ('GET', '/api/trend/backtest-latest'), ('GET', '/api/trend/backtest-records'), ('GET', '/api/stock-diagnose/{symbol}'), ('GET', '/api/stock-map'), ('POST', '/api/update-stock-map'), ('GET', '/api/custom-scan-pool'), ('GET', '/api/custom-scan-pool/symbols'), ('POST', '/api/custom-scan-pool'), ('PUT', '/api/custom-scan-pool/add'), ('PUT', '/api/custom-scan-pool/remove'), ('DELETE', '/api/custom-scan-pool'), ('GET', '/api/hot-stocks-scan'), ('GET', '/api/hot-stocks'), ('POST', '/api/hot-stocks'), ('PUT', '/api/hot-stocks/add'), ('PUT', '/api/hot-stocks/remove'), ('GET', '/api/stock-tags'), ('GET', '/api/stock-tags/{symbol}'), ('POST', '/api/stock-tags/{symbol}'), ('DELETE', '/api/stock-tags/{symbol}'), ('GET', '/api/stock-concepts/{symbol}'), ('GET', '/api/market-predict'), ('GET', '/api/market-alert'), ('GET', '/api/market-index'), ('GET', '/api/action-desk'), ('GET', '/api/theme-index'), ('GET', '/api/cls-ladder'), ('GET', '/api/cls-plate-stocks'), ('GET', '/api/kline/{symbol}'), ('GET', '/api/kline/{symbol}/minute'), ('POST', '/api/daily-review/generate'), ('GET', '/api/daily-reviews'), ('GET', '/api/strategy/weights'), ('POST', '/api/strategy/weights/reset'), ('POST', '/api/strategy/backtest'), ('GET', '/api/strategy/backtest-history'), ('GET', '/api/strategy/predictions'), ('GET', '/api/fund-flow'), ('GET', '/api/rps'), ('POST', '/api/rps/refresh'), ('GET', '/api/kline-history/stats'), ('GET', '/api/internal/ops-notify-limit'), ('GET', '/api/settings'), ('GET', '/api/settings/preview'), ('POST', '/api/settings/{key}'), ('POST', '/api/settings/{key}/reset'), ('POST', '/api/kline-history/sync'), ('POST', '/api/kline-history/backfill'), ('POST', '/api/fundamental/refresh'), ('GET', '/api/dragon-tiger'), ('GET', '/api/northbound'), ('POST', '/api/valuation/refresh'), ('GET', '/api/valuation'), ('GET', '/api/trend/backtest-summary'), ('GET', '/eastmoney/{path:path}'), ('POST', '/eastmoney/{path:path}'), ('GET', '/api/optimization/history'), ('GET', '/api/optimization/status'), ('POST', '/api/optimization/trigger'), ('GET', '/api/optimization/trend')]


def _collect():
    out = []
    for route in R.router.routes:
        methods = getattr(route, "methods", None) or set()
        for m in methods:
            out.append((m.upper(), route.path))
    return out


def main():
    actual = sorted(set(_collect()))
    expected = sorted(set(EXPECTED))
    missing = [e for e in expected if e not in actual]
    extra = [a for a in actual if a not in expected]
    if missing or extra:
        print("FAIL: route set mismatch")
        print("missing:", missing)
        print("extra:", extra)
        print("actual count", len(actual), "expected count", len(expected))
        return 1
    print(f"OK: all {len(actual)} routes preserved after split")
    return 0


if __name__ == "__main__":
    sys.exit(main())
