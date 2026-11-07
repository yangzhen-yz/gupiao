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


def use_db_path(path: str) -> str:
    """把 SQLite 路径整体换掉（自测/离线工具专用），返回真正生效的那个路径。

    为什么需要这个函数：`get_db_conn` 读的是 `db.dal_state.SQLITE_DB_PATH` 这份全局，
    而本模块的 `SQLITE_DB_PATH` 只是 `from .dal_state import *` 复制出来的**副本**。
    2026-11-05 的 database.py 拆分之后，自测里那句
    `db.database.SQLITE_DB_PATH = 临时库` 变成**静默无效**——脚本照样跑绿，但每一行 INSERT
    都写进了生产库 `server/gupiao.db`（11-06 实测：`scripts/fundflow_fallback_selftest.py`
    往真库的 `stock_industry_map` 插测试行，撞到真实数据才以 UNIQUE 失败暴露出来）。
    所以自测换库只许走这里，别直接赋模块变量。
    """
    from . import dal_state
    dal_state.SQLITE_DB_PATH = path
    globals()['SQLITE_DB_PATH'] = path
    return path


def current_db_path() -> str:
    """当前真正生效的库路径（自测用它断言自己没有写生产库）。"""
    from . import dal_state
    return dal_state.SQLITE_DB_PATH
