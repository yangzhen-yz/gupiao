// =============================================================
// 当前市场主线板块词库（2026-08-25）
// 主线 = 当前市场资金集中进攻 / 领导市场方向的板块，会随行情轮动，
// 需根据每日复盘持续维护更新。这里是"按股票名称关键词"的粗粒度匹配，
// 用于给列表页的主线个股打徽标，辅助快速识别。
// -------------------------------------------------------------
// 字段说明：
//   name     板块显示名（徽标上展示的文字）
//   key      唯一标识（预留，可用于样式/扩展）
//   kind     'attack' 进攻/题材主线（暖色）| 'defense' 防守/避险主线（冷色）
//   keywords 命中该板块的股票名称关键词（命中即打徽标）
// =============================================================

export const MAINLINE_SECTORS = [
  // —— 进攻 / 题材主线 ——
  { name: '粮食种业', key: 'agri', kind: 'attack',
    keywords: ['种业', '种苗', '米业', '农发', '粮食', '北大荒', '登海', '荃银', '隆平', '农垦', '农化', '金健'] },
  { name: '家电', key: 'appliance', kind: 'attack',
    keywords: ['美的', '格力', '海尔', '海信', '石头', '九阳', '苏泊尔', '老板电器', '小天鹅'] },
  { name: '医药商业', key: 'pharma', kind: 'attack',
    keywords: ['药房', '人民同泰', '大参林', '一心堂'] },
  { name: '半导体存储', key: 'semi', kind: 'attack',
    keywords: ['半导体', '芯片', '集成电路', '存储', '中芯', '长电', '兆易', '北方华创', '韦尔'] },
  // —— 防守 / 避险主线 ——
  { name: '黄金贵金属', key: 'gold', kind: 'defense',
    keywords: ['黄金', '白银', '贵金属', '金矿', '紫金', '银业', '铜业', '金业', '珠宝'] },
  { name: '煤炭', key: 'coal', kind: 'defense',
    keywords: ['煤炭', '煤业', '焦煤', '神华', '平煤', '新集', '潞安', '兖矿', '上海能源', '山煤'] },
  { name: '银行', key: 'bank', kind: 'defense',
    keywords: ['银行'] },
]

/**
 * 根据股票名称判断其是否属于当前市场主线。
 * @param {string} name 股票名称
 * @returns {{sector:string, key:string, kind:string} | null} 命中返回板块信息，否则 null
 */
export function matchMainLine(name) {
  if (!name) return null
  const n = String(name)
  for (const sec of MAINLINE_SECTORS) {
    if (sec.keywords.some(k => n.includes(k))) {
      return { sector: sec.name, key: sec.key, kind: sec.kind }
    }
  }
  return null
}