"""Application configuration facade.

The former monolithic config.py (1574 lines) was split into thematic
submodules (cfg_*). This module re-exports everything via `from .cfg_X import *`
so existing `from app.config import X` callers keep working unchanged.
"""

from .cfg_db import *
from .cfg_api import *
from .cfg_hot import *
from .cfg_fundflow import *
from .cfg_scan import *
from .cfg_trend import *
from .cfg_strategy import *
from .cfg_market import *
from .cfg_universe import *
from .cfg_schedule import *
from .cfg_recommend import *
from .cfg_backtest import *
from .cfg_review import *
from .cfg_auth import *
from .cfg_wecom import *
