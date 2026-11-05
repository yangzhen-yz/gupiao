"""Domain router: account."""

from fastapi import APIRouter
from ..routes_state import *

router = APIRouter()

@router.get("/api/positions", tags=["持仓管理"])
async def get_positions_api(user: Dict = Depends(current_user)):
    """持仓诊断：实时盈亏 + 单票动作（止损/止盈/减仓/持有）+ 组合仓位建议（按登录用户隔离）"""
    try:
        from services.portfolio.position import get_portfolio_diagnosis
        return {"success": True, "data": await get_portfolio_diagnosis(user_id=user['id'])}
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"获取持仓诊断失败: {str(error)}")


@router.post("/api/positions", tags=["持仓管理"])
async def save_position_api(pos: Dict, user: Dict = Depends(current_user)):
    """新增/更新持仓（symbol 唯一，重复提交同一 symbol 为更新）

    2026-10-12 加固：代码格式校验 + 代码/名称互相补全。
    - 接受 sh600000 / sz000001 / 纯6位（自动补 sh/sz 前缀）
    - 代码非法但有名称 → 按名称反查代码（用户记不住编码）
    - 缺名称 → 按代码自动补全名称
    """
    try:
        symbol = (pos.get('symbol') or '').strip().lower()
        name = (pos.get('name') or '').strip()
        cost = float(pos.get('cost_price', 0) or 0)
        shares = int(pos.get('shares', 0) or 0)
        if cost <= 0 or shares <= 0:
            raise HTTPException(status_code=400, detail="成本价与股数必须大于0")

        # 纯6位代码自动补交易所前缀（6→沪，0/3→深，4/8→北）
        if re.fullmatch(r'\d{6}', symbol):
            if symbol[0] == '6':
                symbol = 'sh' + symbol
            elif symbol[0] in '03':
                symbol = 'sz' + symbol
            else:
                symbol = 'bj' + symbol

        if not re.fullmatch(r'(sh|sz|bj)\d{6}', symbol):
            # 代码非法：尝试反查代码（优先用名称字段；没传名称时把输入本身当名称兜底——
            # 覆盖"用户输了名称但没从下拉框选"的场景）
            resolved = ''
            lookup_name = name or symbol
            if lookup_name:
                basic = load_stock_basic_info()
                alias = load_stock_alias_map()
                for sym, nm in basic.items():
                    if nm == lookup_name:
                        resolved = sym
                        break
                if not resolved and lookup_name in alias:
                    resolved = alias[lookup_name].lower()
            if resolved:
                symbol = resolved
            else:
                detail = (f'股票代码"{pos.get("symbol")}"格式不正确（应为 sh600000 / sz000001，或直接输6位代码）'
                          if not lookup_name else
                          f'代码/名称"{lookup_name}"未匹配到股票，请从下拉列表选择或检查输入')
                raise HTTPException(status_code=400, detail=detail)

        # 缺名称时按代码补全；basic_info 有权威名时覆盖传入名（防别名/简称/手输错误名入库）
        authoritative = load_stock_basic_info().get(symbol, '')
        if authoritative:
            name = authoritative
        elif not name:
            try:
                name = await get_stock_name(symbol) or symbol
            except Exception:
                name = symbol

        pos['symbol'] = symbol
        pos['name'] = name
        # 流水留痕（2026-11-04）：这个接口是"symbol 重复即更新"，所以 create/edit 只能靠
        # **保存前先查一次旧持仓**来区分。两者都记但 source 不同——回测只取 create+adjust_*，
        # edit 是改历史，当成买卖会造出根本不存在的成交（这一点在表注释里也写了）。
        from datetime import datetime as _dt
        _old_pos = None
        try:
            from db.database import load_user_positions
            _old_pos = next((p for p in load_user_positions(user_id=user['id'])
                             if (p.get('symbol') or '').lower() == symbol), None)
        except Exception as _q_err:
            print(f'[流水] 查旧持仓失败（按 create 记）: {_q_err}')
        from db.database import save_user_position
        if not save_user_position(pos, user_id=user['id']):
            raise HTTPException(status_code=500, detail="保存失败")
        try:
            from db.database import save_position_trade_log
            save_position_trade_log({
                'trade_date': _dt.now().strftime('%Y-%m-%d'),
                'symbol': symbol, 'name': name,
                'source': 'edit' if _old_pos else 'create',
                'shares_before': int(_old_pos.get('shares') or 0) if _old_pos else None,
                'price': cost, 'qty': shares,
                'shares_after': shares, 'cost_price_after': cost,
                'realized_pnl': None,
                'note': ('编辑持仓' if _old_pos
                         else '首次录入（price=录入成本价，属近似成交价）'),
            }, user_id=user['id'])
        except Exception as _log_err:
            print(f'[流水] save 留痕失败（不影响持仓）: {_log_err}')
        return {"success": True, "message": "持仓已保存", "data": {"symbol": symbol, "name": name}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"保存持仓失败: {str(error)}")


@router.post("/api/positions/capital", tags=["持仓管理"])
async def save_capital_api(body: Dict, user: Dict = Depends(current_user)):
    """设置本账号初始本金（仓位口径的分母）

    没有本金，"仓位"就只能拿持仓市值自己除自己 —— 只持一只票就永远是 100%，
    超配告警和建议总仓位也全都失去意义。填一次本金后，单票仓位与总仓位才按真实资金占比算。
    principal 传 0 表示清除设置，回到旧的"按总成本"口径。
    """
    try:
        raw = body.get('principal', None)
        if raw is None or str(raw).strip() == '':
            raise HTTPException(status_code=400, detail="请提供本金金额 principal")
        try:
            principal = round(float(raw), 2)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="本金必须是数字")
        if principal < 0:
            raise HTTPException(status_code=400, detail="本金不能为负")
        if principal > 1e12:
            raise HTTPException(status_code=400, detail="本金数额过大，请确认单位是否为元")

        from db.database import save_user_capital
        if not save_user_capital(principal, user_id=user['id']):
            raise HTTPException(status_code=500, detail="保存失败")
        return {"success": True,
                "message": ("已清除本金设置" if principal <= 0 else f"初始本金已设为 {principal:,.2f} 元"),
                "data": {"principal": principal}}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"保存本金失败: {str(error)}")


@router.get("/api/push-channels", tags=["推送设置"])
async def list_push_channels_api(user: Dict = Depends(current_user)):
    from services.support.push_channel import list_channels, sent_today
    from app.config import PUSH_DAILY_LIMIT_PER_USER
    import services.support.push_channel as pc
    return {"success": True, "data": {
        "channels": list_channels(user['id']),
        "sent_today": sent_today(user['id']),
        "daily_limit": PUSH_DAILY_LIMIT_PER_USER,
        # 一条都没绑时消息会落到运维者的全局群；把这件事摆在明面上，不藏着
        "using_global_fallback": bool(pc._global_fallback_url()) and not list_channels(user['id']),
    }}


@router.post("/api/push-channels", tags=["推送设置"])
async def bind_push_channel_api(body: Dict, user: Dict = Depends(current_user)):
    from services.support.push_channel import bind_webhook
    url = str(body.get('url') or '').strip()
    label = str(body.get('label') or '').strip()
    if not url:
        raise HTTPException(status_code=400, detail="请粘贴群机器人的 Webhook 地址")
    ok, msg, cid = bind_webhook(user['id'], url, label)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg, "data": {"id": cid}}


@router.get("/api/push-channels/discover", tags=["推送设置"])
async def discover_push_sessions_api(user: Dict = Depends(current_user)):
    """列出机器人最近的会话：给机器人发一句话，再刷新这里，认领自己那条即可绑定单聊。

    不需要公网域名、不需要管理员建应用 —— 智能机器人的授权走腾讯托管页面，
    本地 CLI 轮询取结果，所以回调和域名都没关系。
    2026-10-22：列表含全部会话（姓名+chat_id），只有管理员（yz）可刷新，
    普通账号认领走"手填 chat_id"或让管理员代认领。
    """
    if not user.get('isAdmin'):
        raise HTTPException(status_code=403, detail='机器人会话列表仅管理员可查看/刷新')
    from services.support.push_channel import discover_sessions
    ok, err, items = discover_sessions()
    return {"success": ok, "data": {"sessions": items},
            **({} if ok else {"error": err, "detail": err})}


@router.post("/api/push-channels/search-user", tags=["推送设置"])
async def search_push_user_api(body: Dict, user: Dict = Depends(current_user)):
    """按姓名查企微成员 userid（需机器人「通讯录」权限；没授权时返回带授权链接的错误）
    与 discover 同理：查的是企业通讯录，仅管理员可用。"""
    if not user.get('isAdmin'):
        raise HTTPException(status_code=403, detail='通讯录查询仅管理员可用')
    from services.support.push_channel import search_users
    ok, err, items = search_users(str(body.get('keyword') or ''))
    return {"success": ok, "data": {"users": items},
            **({} if ok else {"error": err, "detail": err})}


@router.post("/api/push-channels/aibot", tags=["推送设置"])
async def bind_push_aibot_api(body: Dict, user: Dict = Depends(current_user)):
    """绑定智能机器人单聊（chat_id 来自 discover 列表或手填），实发确认消息成功才落库"""
    from services.support.push_channel import bind_aibot
    chat_id = str(body.get('chat_id') or '').strip()
    if not chat_id:
        raise HTTPException(status_code=400, detail="请先给机器人发一句话，再在列表里认领会话")
    ok, msg, cid = bind_aibot(user['id'], chat_id, str(body.get('label') or '').strip())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg, "data": {"id": cid}}


@router.get("/api/push-channels/agent/status", tags=["推送设置"])
async def push_agent_status_api(user: Dict = Depends(current_user)):
    """自建应用（单聊）通道的就绪自检：缺哪个参数、token 取不取得到、还差管理员哪一步"""
    from services.support.push_channel import agent_status
    return {"success": True, "data": agent_status()}


@router.post("/api/push-channels/agent", tags=["推送设置"])
async def bind_push_agent_api(body: Dict, user: Dict = Depends(current_user)):
    """绑定企业微信「自建应用」单聊：填自己的企微账号ID，服务端实发一条确认消息才算成功"""
    from services.support.push_channel import bind_userid
    userid = str(body.get('userid') or '').strip()
    if not userid:
        raise HTTPException(status_code=400, detail="请填写企业微信通讯录里的「账号」(userid)")
    ok, msg, cid = bind_userid(user['id'], userid, str(body.get('label') or '').strip())
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"success": True, "message": msg, "data": {"id": cid}}


@router.post("/api/push-channels/{ch_id}/test", tags=["推送设置"])
async def test_push_channel_api(ch_id: int, user: Dict = Depends(current_user)):
    from services.support.push_channel import test_channel
    ok, msg = test_channel(user['id'], ch_id)
    if not ok:
        raise HTTPException(status_code=400, detail=msg or '发送失败')
    return {"success": True, "message": "测试消息已发出，请到企业微信查看"}


@router.delete("/api/push-channels/{ch_id}", tags=["推送设置"])
async def unbind_push_channel_api(ch_id: int, user: Dict = Depends(current_user)):
    from services.support.push_channel import unbind
    if not unbind(user['id'], ch_id):
        raise HTTPException(status_code=404, detail="通道不存在或不属于本账号")
    return {"success": True, "message": "已解绑"}


@router.post("/api/positions/adjust", tags=["持仓管理"])
async def adjust_position_api(body: Dict, user: Dict = Depends(current_user)):
    """加仓/减仓：加权成本由服务端算（口径见 services/position_adjust）"""
    from datetime import datetime
    from db.database import load_user_positions, save_user_position, delete_user_position
    from services.portfolio.position_adjust import apply_adjust, describe

    symbol = (body.get('symbol') or '').strip().lower()
    if not re.fullmatch(r'(sh|sz|bj)\d{6}', symbol):
        raise HTTPException(status_code=400, detail="股票代码格式应为 sh600000 / sz000001")
    try:
        delta = int(float(body.get('shares', 0) or 0))
        price = round(float(body.get('price', 0) or 0), 3)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="股数与成交价必须是数字")

    cur = next((p for p in load_user_positions(user_id=user['id'])
                if (p.get('symbol') or '').lower() == symbol), None)
    if not cur:
        raise HTTPException(status_code=404, detail="没有找到这条持仓，请先添加")

    try:
        res = apply_adjust(cur, (body.get('mode') or '').strip(), delta, price,
                           datetime.now().strftime('%Y-%m-%d'))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if res['cleared']:
        if not delete_user_position(symbol, user_id=user['id']):
            raise HTTPException(status_code=500, detail="清仓失败")
    else:
        pos = {'symbol': symbol, 'name': cur.get('name') or symbol,
               'cost_price': res['cost_price'], 'shares': res['shares'],
               'buy_date': cur.get('buy_date') or '', 'note': res['note']}
        if not save_user_position(pos, user_id=user['id']):
            raise HTTPException(status_code=500, detail="保存失败")

    # 已实现盈亏落账（2026-10-23）：减仓/清仓赚的 money 不能只活在 note 文本里——
    # 清仓会整行删除持仓，不落数的话「当前资金/总盈亏」永远停在初始本金。
    realized_total = None
    if res['realized_pnl'] is not None:
        from db.database import add_user_realized_pnl
        realized_total = add_user_realized_pnl(res['realized_pnl'], user_id=user['id'])

    # 流水留痕（2026-11-04）：只追加一张旁路表，不影响本接口的返回值、持仓或已实现盈亏。
    # source 分 add/reduce/clear 三种——清仓要单独记，否则回测看不出持仓是哪一天消失的。
    # 整块包 try：留痕写失败的代价上限是"少一条审计"，绝不能把用户的加减仓操作带崩。
    try:
        from db.database import save_position_trade_log
        _mode = (body.get('mode') or '').strip()
        save_position_trade_log({
            'trade_date': datetime.now().strftime('%Y-%m-%d'),
            'symbol': symbol, 'name': cur.get('name') or symbol,
            'source': ('adjust_clear' if res['cleared']
                       else 'adjust_add' if _mode == 'add' else 'adjust_reduce'),
            'shares_before': int(cur.get('shares') or 0),
            'price': price, 'qty': delta,
            'shares_after': 0 if res['cleared'] else res['shares'],
            'cost_price_after': None if res['cleared'] else res['cost_price'],
            'realized_pnl': res['realized_pnl'],
            'note': describe(res, cur.get('name') or symbol) or '',
        }, user_id=user['id'])
    except Exception as _log_err:
        print(f'[流水] adjust 留痕失败（不影响持仓）: {_log_err}')

    msg = describe(res, cur.get('name') or symbol)
    return {"success": True, "message": msg,
            "data": {"symbol": symbol, 'cleared': res['cleared'],
                     'cost_price': res['cost_price'], 'shares': res['shares'],
                     'realized_pnl': res['realized_pnl'],
                     'realized_total': realized_total}}


@router.delete("/api/positions/{symbol}", tags=["持仓管理"])
async def delete_position_api(symbol: str, user: Dict = Depends(current_user)):
    """删除持仓（只能删自己的）"""
    try:
        from db.database import delete_user_position
        # 流水留痕（2026-11-04）：删除到底是"清仓"还是"删掉一条记错的记录"，代码层面区分不了，
        # 所以 source='delete' **只作审计、回测不许用**（load_position_trade_log 的 trade_only
        # 口径里就没有它）。要真清仓请走 /api/positions/adjust 的减仓到 0，那条会记 adjust_clear。
        from datetime import datetime as _dt
        _old_pos = None
        try:
            from db.database import load_user_positions
            _old_pos = next((p for p in load_user_positions(user_id=user['id'])
                             if (p.get('symbol') or '').lower() == symbol.lower()), None)
        except Exception as _q_err:
            print(f'[流水] 删除前查旧持仓失败: {_q_err}')
        if not delete_user_position(symbol, user_id=user['id']):
            raise HTTPException(status_code=404, detail="未找到该持仓")
        try:
            from db.database import save_position_trade_log
            save_position_trade_log({
                'trade_date': _dt.now().strftime('%Y-%m-%d'),
                'symbol': symbol, 'name': (_old_pos or {}).get('name') or symbol,
                'source': 'delete',
                'shares_before': int((_old_pos or {}).get('shares') or 0),
                'price': (_old_pos or {}).get('cost_price'),
                'qty': None, 'shares_after': 0,
                'cost_price_after': None, 'realized_pnl': None,
                'note': '删除记录（非交易动作，回测不计）',
            }, user_id=user['id'])
        except Exception as _log_err:
            print(f'[流水] delete 留痕失败（不影响持仓）: {_log_err}')
        return {"success": True, "message": "持仓已删除"}
    except HTTPException:
        raise
    except Exception as error:
        raise HTTPException(status_code=500, detail=f"删除持仓失败: {str(error)}")

