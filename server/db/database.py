"""Database access layer facade.

The former monolithic database.py (3442 lines) was split into ``dal_state`` (shared
state/connection/cache + cross-domain functions) and ``dal/*`` domain modules. This
module re-exports everything so existing ``from server.db.database import X`` callers
keep working unchanged.
"""

from .dal_state import *
from .dal.fundamental import *
from .dal.hot import *
from .dal.market import *
from .dal.recommend import *
from .dal.stock import *
from .dal.strategy import *
from .dal.trend import *
from .dal.user import *
from .dal.schema import *
from .dal.usage import *
