"""API route aggregator.

The former monolithic routes.py was split into per-domain routers under
``./routers``. This module wires them together and re-exports shared
helpers from ``routes_state`` so existing ``from app.api.routes import ...``
callers keep working.
"""

from fastapi import APIRouter
from .routers import (
    account,
    auth,
    data,
    eval as eval_router,
    hot,
    kline,
    map as map_router,
    market,
    recommend,
    review,
    scan,
    settings,
    sim,
    stock,
    strategy,
    system,
    tags,
    trend,
)

router = APIRouter()
router.include_router(account.router)
router.include_router(auth.router)
router.include_router(data.router)
router.include_router(eval_router.router)
router.include_router(hot.router)
router.include_router(kline.router)
router.include_router(map_router.router)
router.include_router(market.router)
router.include_router(recommend.router)
router.include_router(review.router)
router.include_router(scan.router)
router.include_router(settings.router)
router.include_router(sim.router)
router.include_router(stock.router)
router.include_router(strategy.router)
router.include_router(system.router)
router.include_router(tags.router)
router.include_router(trend.router)

# Re-export shared helpers/caches so legacy `from app.api.routes import X` works.
from .routes_state import *  # noqa: F401,F403
