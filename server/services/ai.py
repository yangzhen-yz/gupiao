"""ai_diagnose.py - split from main.py"""
import json, httpx, os
from typing import List, Dict, Any, Optional
from app.config import AI_CONFIG_FILE, INDEX_SYMBOLS

# ========== DeepSeek AI 配置 ==========
# ========== DeepSeek AI 配置 ==========
DEEPSEEK_API_KEY = os.environ.get('DEEPSEEK_API_KEY', '')
DEEPSEEK_BASE_URL = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
DEEPSEEK_MODEL = os.environ.get('DEEPSEEK_MODEL', 'DeepSeek-V4-Flash')
AI_CONFIG_MTIME = 0.0

def load_ai_config():
    global DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL, AI_CONFIG_MTIME
    try:
        if not os.path.exists(AI_CONFIG_FILE):
            return
        mtime = os.path.getmtime(AI_CONFIG_FILE)
        if mtime == AI_CONFIG_MTIME:
            return
        AI_CONFIG_MTIME = mtime
        with open(AI_CONFIG_FILE, 'r', encoding='utf-8') as f:
            config = json.load(f)
            key = (config.get('apiKey') or '').strip()
            url = (config.get('baseUrl') or '').strip()
            model = (config.get('model') or '').strip()
            if key and not key.startswith('在'):
                DEEPSEEK_API_KEY = key
            if url:
                DEEPSEEK_BASE_URL = url
            if model:
                DEEPSEEK_MODEL = model
        if DEEPSEEK_API_KEY:
            print(f'[AI配置] 已加载，模型: {DEEPSEEK_MODEL}')
        else:
            print(f'[AI配置] 未配置API Key，请编辑 server/ai_config.json')
    except Exception as e:
        print(f'[AI配置] 加载失败: {str(e)}')


def _calc_rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

def _calc_kdj(kline_data: List[List], n: int = 9) -> Dict:
    if len(kline_data) < n:
        return {'K': None, 'D': None, 'J': None}
    closes = [float(k[2]) for k in kline_data]
    highs = [float(k[3]) for k in kline_data]
    lows = [float(k[4]) for k in kline_data]
    recent_closes = closes[-n:]
    recent_highs = highs[-n:]
    recent_lows = lows[-n:]
    hn = max(recent_highs)
    ln = min(recent_lows)
    rsv = ((closes[-1] - ln) / (hn - ln) * 100) if (hn - ln) > 0 else 50
    k = 2 / 3 * 50 + 1 / 3 * rsv
    d = 2 / 3 * 50 + 1 / 3 * k
    j = 3 * k - 2 * d
    return {'K': round(k, 2), 'D': round(d, 2), 'J': round(j, 2)}

def _calc_macd(closes: List[float], short: int = 12, long: int = 26, signal: int = 9) -> Dict:
    if len(closes) < long + signal:
        return {'dif': None, 'dea': None, 'macd': None}
    ema_short = closes[0]
    ema_long = closes[0]
    dif_list = []
    for c in closes[1:]:
        ema_short = (2 / (short + 1)) * c + (short - 1) / (short + 1) * ema_short
        ema_long = (2 / (long + 1)) * c + (long - 1) / (long + 1) * ema_long
        dif_list.append(ema_short - ema_long)
    dea = dif_list[0]
    for d in dif_list[1:]:
        dea = (2 / (signal + 1)) * d + (signal - 1) / (signal + 1) * dea
    macd_val = 2 * (dif_list[-1] - dea)
    return {
        'dif': round(dif_list[-1], 3),
        'dea': round(dea, 3),
        'macd': round(macd_val, 3)
    }

def _detect_kline_pattern(kline_data: List[List]) -> List[str]:
    patterns = []
    if len(kline_data) < 3:
        return patterns
    recent = kline_data[-3:]
    c0 = [float(recent[0][2]), float(recent[0][3]), float(recent[0][4]), float(recent[0][1])]
    c1 = [float(recent[1][2]), float(recent[1][3]), float(recent[1][4]), float(recent[1][1])]
    c2 = [float(recent[2][2]), float(recent[2][3]), float(recent[2][4]), float(recent[2][1])]
    body2 = abs(c2[0] - c2[3])
    upper2 = c2[1] - max(c2[0], c2[3])
    lower2 = min(c2[0], c2[3]) - c2[2]
    total2 = c2[1] - c2[2] if c2[1] != c2[2] else 0.01
    if body2 / total2 < 0.15 and upper2 > body2 * 2 and lower2 > body2 * 2:
        patterns.append('十字星(变盘信号)')
    if c2[0] > c2[3] and c1[0] > c1[3] and c0[0] < c0[3]:
        if c0[3] > c1[0]:
            patterns.append('看跌吞没(看空)')
    if c2[0] < c2[3] and c1[0] < c1[3] and c0[0] > c0[3]:
        if c0[3] < c1[0]:
            patterns.append('看涨吞没(看多)')
    if c2[0] > c2[3] and c1[0] < c1[3] and c0[0] < c0[3]:
        if c1[3] < c2[3] and c0[3] < c2[3]:
            patterns.append('黄昏之星(看空)')
    if c2[0] < c2[3] and c1[0] > c1[3] and c0[0] > c0[3]:
        if c1[3] > c2[3] and c0[3] > c2[3]:
            patterns.append('晨星(看多)')
    if c2[0] > c2[3] and body2 / total2 > 0.6 and lower2 < body2 * 0.1:
        patterns.append('光头大阳线(强势)')
    if c2[0] < c2[3] and body2 / total2 > 0.6 and upper2 < body2 * 0.1:
        patterns.append('光头大阴线(弱势)')
    return patterns

def _calc_support_resistance(kline_data: List[List]) -> Dict:
    if len(kline_data) < 10:
        return {'support': [], 'resistance': []}
    closes = [float(k[2]) for k in kline_data]
    highs = [float(k[3]) for k in kline_data]
    lows = [float(k[4]) for k in kline_data]
    recent = kline_data[-30:] if len(kline_data) >= 30 else kline_data
    current_price = closes[-1]
    supports = []
    resistances = []
    for i in range(1, len(recent) - 1):
        lo = float(recent[i][4])
        hi = float(recent[i][3])
        prev_lo = float(recent[i - 1][4])
        next_lo = float(recent[i + 1][4]) if i + 1 < len(recent) else lo
        prev_hi = float(recent[i - 1][3])
        next_hi = float(recent[i + 1][3]) if i + 1 < len(recent) else hi
        if lo < prev_lo and lo < next_lo and lo < current_price:
            supports.append(round(lo, 2))
        if hi > prev_hi and hi > next_hi and hi > current_price:
            resistances.append(round(hi, 2))
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10 if len(closes) >= 10 else None
    ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
    if ma5 and ma5 < current_price:
        supports.append(round(ma5, 2))
    elif ma5 and ma5 > current_price:
        resistances.append(round(ma5, 2))
    if ma10:
        if ma10 < current_price:
            supports.append(round(ma10, 2))
        else:
            resistances.append(round(ma10, 2))
    if ma20:
        if ma20 < current_price:
            supports.append(round(ma20, 2))
        else:
            resistances.append(round(ma20, 2))
    supports = sorted(set(supports), reverse=True)[:3]
    resistances = sorted(set(resistances))[:3]
    return {'support': supports, 'resistance': resistances}
def _build_diagnosis_prompt(stock_info: Dict, kline_data: List, market_data: Dict) -> str:
    name = stock_info.get('name', '')
    symbol = stock_info.get('symbol', '')
    price = stock_info.get('price', 0)
    change_pct = stock_info.get('changePercent', 0)
    open_price = stock_info.get('open', 0)
    high = stock_info.get('high', 0)
    low = stock_info.get('low', 0)
    volume = stock_info.get('volume', 0)
    turnover = stock_info.get('turnover', 0)
    turnover_rate = stock_info.get('turnoverRate', 0)
    volume_ratio = stock_info.get('volumeRatio', 0)
    pe = stock_info.get('pe', 0)
    outer_plate = stock_info.get('outerPlate', 0)
    inner_plate = stock_info.get('innerPlate', 0)
    weibi = stock_info.get('weibi', 0)
    amplitude = stock_info.get('amplitude', 0)
    avg_price = stock_info.get('avgPrice', 0)
    avg_price_deviation = stock_info.get('avgPriceDeviation', 0)
    circulate_market_cap = stock_info.get('circulateMarketCap', 0)
    bid1 = stock_info.get('bid1', 0)
    bid1_vol = stock_info.get('bid1Vol', 0)
    ask1 = stock_info.get('ask1', 0)
    ask1_vol = stock_info.get('ask1Vol', 0)
    yesterday_close = stock_info.get('yesterdayClose', 0)

    outer_ratio = (outer_plate / (outer_plate + inner_plate) * 100) if (outer_plate + inner_plate) > 0 else 50
    plate_diff = outer_plate - inner_plate

    tech_indicators = ""
    sr_levels = {'support': [], 'resistance': []}
    kline_patterns = []
    if kline_data and len(kline_data) > 0:
        closes = [float(k[2]) for k in kline_data]
        recent = kline_data[-20:] if len(kline_data) >= 20 else kline_data
        kline_summary = "\n".join([
            f"  {k[0]}: 开{k[1]} 收{k[2]} 高{k[3]} 低{k[4]} 量{k[5] if len(k) > 5 else '-'}"
            for k in recent
        ])
        ma5_vals = calculate_ma(kline_data, 5)
        ma10_vals = calculate_ma(kline_data, 10)
        ma20_vals = calculate_ma(kline_data, 20)
        ma60_vals = calculate_ma(kline_data, 60)
        latest_idx = len(kline_data) - 1
        kline_summary += f"\n  MA5={ma5_vals[latest_idx]} MA10={ma10_vals[latest_idx]} MA20={ma20_vals[latest_idx]} MA60={ma60_vals[latest_idx] if ma60_vals[latest_idx] else 'N/A'}"

        consecutive_up = 0
        for i in range(latest_idx, 0, -1):
            if closes[i] > closes[i - 1]:
                consecutive_up += 1
            else:
                break
        consecutive_down = 0
        for i in range(latest_idx, 0, -1):
            if closes[i] < closes[i - 1]:
                consecutive_down += 1
            else:
                break
        kline_summary += f"\n  连涨天数={consecutive_up} 连跌天数={consecutive_down}"

        rsi = _calc_rsi(closes)
        kdj = _calc_kdj(kline_data)
        macd = _calc_macd(closes)
        sr_levels = _calc_support_resistance(kline_data)
        kline_patterns = _detect_kline_pattern(kline_data)

        tech_indicators = f"""
## 技术指标
- RSI(14): {rsi if rsi is not None else 'N/A'}
- KDJ: K={kdj['K']} D={kdj['D']} J={kdj['J']}
- MACD: DIF={macd['dif']} DEA={macd['dea']} MACD柱={macd['macd']}
- K线形态: {', '.join(kline_patterns) if kline_patterns else '无明显形态'}
- 支撑位: {', '.join(map(str, sr_levels['support'])) if sr_levels['support'] else '暂无'}
- 压力位: {', '.join(map(str, sr_levels['resistance'])) if sr_levels['resistance'] else '暂无'}
"""
    else:
        kline_summary = "  无K线数据"

    market_summary = ""
    market_trend = ""
    for key, info in market_data.items():
        m_name = info.get('name', key)
        m_price = info.get('price', 0)
        m_change = info.get('changePercent', 0)
        market_summary += f"\n  {m_name}: {m_price} ({m_change:+.2f}%)"
    market_changes = [info.get('changePercent', 0) for info in market_data.values()]
    if market_changes:
        avg_market = sum(market_changes) / len(market_changes)
        if avg_market > 0.5:
            market_trend = "大盘强势上涨，市场情绪偏多"
        elif avg_market > 0:
            market_trend = "大盘小幅上涨，市场情绪中性偏多"
        elif avg_market > -0.5:
            market_trend = "大盘小幅下跌，市场情绪中性偏空"
        else:
            market_trend = "大盘明显下跌，市场情绪偏空"

    prompt = f"""你是一位资深的A股量化分析师，擅长从多维度综合分析个股走势，给出有实战价值的操作建议。

## 股票基本信息
- 名称: {name} ({symbol})
- 当前价: {price} | 昨收: {yesterday_close}
- 涨跌幅: {change_pct:+.2f}% | 振幅: {amplitude:.2f}%
- 今开: {open_price} | 最高: {high} | 最低: {low}

## 成交数据
- 成交量: {volume}手 | 成交额: {turnover/10000:.2f}万
- 换手率: {turnover_rate:.2f}% | 量比: {volume_ratio:.2f}
- 流通市值: {circulate_market_cap/10000:.2f}亿

## 主力资金数据
- 外盘(主动买): {outer_plate} | 内盘(主动卖): {inner_plate}
- 外盘占比: {outer_ratio:.1f}% | 内外盘差: {plate_diff}
- 委比: {weibi:+.2f}%（注意：委比可能存在虚假挂单，需结合涨跌和量比验证）
- 买一: {bid1}({bid1_vol}手) | 卖一: {ask1}({ask1_vol}手)

## 均价分析
- 均价: {avg_price} | 偏离均价: {avg_price_deviation:+.2f}%

## 近20日K线数据
{kline_summary}
{tech_indicators}
## 大盘环境
{market_summary}
市场整体判断: {market_trend}

## 基本面
- 市盈率: {pe if pe > 0 else '亏损'}

---

## 分析要求

请严格按照以下JSON格式输出分析结果（不要输出其他内容）：

{{
  "direction": "方向，必须是以下之一：强烈买入/买入/轻仓关注/观望/减仓/卖出",
  "confidence": 75,
  "summary": "一句话总结核心观点，要具体有数据支撑",
  "scores": {{
    "volume": 0,
    "capital": 0,
    "technique": 0,
    "market": 0,
    "fundamental": 0
  }},
  "analysis": {{
    "volume": "成交量分析：结合量比、换手率、成交额变化，判断资金参与度",
    "capital": "主力资金分析：结合外盘占比、委比（注意虚假挂单）、内外盘差，判断主力意图",
    "technique": "技术面分析：结合均线排列、MACD/RSI/KDJ指标、K线形态、支撑压力位，判断趋势方向",
    "market": "大盘环境分析：大盘走势对个股的影响",
    "fundamental": "基本面分析：市盈率、流通市值的合理性"
  }},
  "keySignals": {{
    "bullish": ["看多信号1", "看多信号2"],
    "bearish": ["看空信号1", "看空信号2"]
  }},
  "triggerCondition": "转为买入/加仓的具体条件（如：放量突破XX元、RSI回落至30以下等）",
  "risk": "主要风险提示",
  "suggestion": "具体操作建议（含仓位比例和关键价位）"
}}

## 重要规则：
1. direction不要默认给"观望"，必须根据数据给出有倾向性的判断
2. 如果多数指标偏多但有个别风险，应给"买入"或"轻仓关注"而非"观望"
3. 如果连跌多日出现缩量企稳+RSI超卖，应给"轻仓关注"而非"卖出"
4. 如果放量上涨+外盘占优+均线多头，应给"买入"或"强烈买入"
5. scores中每项0-100分，50为中性，>60偏多，<40偏空
6. keySignals必须列出至少1个看多和1个看空信号
7. triggerCondition必须给出具体的价位或指标条件"""

    return prompt
async def _call_deepseek_api(prompt: str) -> str:
    if not DEEPSEEK_API_KEY:
        raise HTTPException(status_code=400, detail="未配置DeepSeek API Key，请设置环境变量 DEEPSEEK_API_KEY")

    client = get_http_client()
    url = f"{DEEPSEEK_BASE_URL}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": [
            {"role": "system", "content": "你是专业的A股量化分析师，擅长根据成交量、主力资金、技术面、基本面和大盘环境给出精准的买卖建议。请始终以JSON格式回复。"},
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.3,
        "max_tokens": 1500,
        "response_format": {"type": "json_object"}
    }

    try:
        response = await client.post(url, headers=headers, json=payload, timeout=30.0)
        response.raise_for_status()
        result = response.json()
        content = result['choices'][0]['message']['content']
        return content
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="AI诊断超时，请稍后重试")
    except httpx.HTTPStatusError as e:
        detail = f"DeepSeek API错误: {e.response.status_code}"
        try:
            err_body = e.response.json()
            detail = f"DeepSeek API错误: {err_body.get('error', {}).get('message', str(e.response.status_code))}"
        except:
            pass
        raise HTTPException(status_code=502, detail=detail)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"AI诊断失败: {str(e)}")
