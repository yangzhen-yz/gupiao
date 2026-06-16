// 综合评分计算
export function calculateScore(data) {
  const outerRatio = data.outerRatio || 50
  const turnoverRate = data.turnoverRate || 0
  const volumeRatio = data.volumeRatio || 0
  const weibi = data.weibi || 0
  const avgPriceDeviation = data.avgPriceDeviation || 0
  const amplitude = data.amplitude || 0
  const changePercent = data.changePercent || 0
  const pe = data.pe || 0

  const details = []

  let scoreOuter = 5
  if (outerRatio >= 60) scoreOuter = 20
  else if (outerRatio >= 55) scoreOuter = 15
  else if (outerRatio >= 50) scoreOuter = 10
  details.push({ label: `外盘占比(${outerRatio.toFixed(1)}%)`, score: scoreOuter, max: 20 })

  let scoreVolume = 5
  if (volumeRatio > 1.8 && outerRatio < 52) scoreVolume = 0
  else if (volumeRatio >= 1.5) scoreVolume = outerRatio > 55 ? 10 : 5
  else if (volumeRatio >= 1) scoreVolume = 15
  else if (volumeRatio >= 0.5) scoreVolume = 8
  details.push({ label: `量比(${volumeRatio.toFixed(2)})`, score: scoreVolume, max: 15 })

  let scoreTurnover = 3
  if (turnoverRate >= 3 && turnoverRate <= 10) scoreTurnover = 10
  else if (turnoverRate >= 1 && turnoverRate < 3) scoreTurnover = 7
  else if (turnoverRate > 10) scoreTurnover = 5
  details.push({ label: `换手率(${turnoverRate.toFixed(2)}%)`, score: scoreTurnover, max: 10 })

  let scoreChange = 0
  if (changePercent > 9.5) scoreChange = 5
  else if (changePercent > 7) scoreChange = 12
  else if (changePercent > 3) scoreChange = (volumeRatio >= 1.2 && outerRatio > 58) ? 20 : 15
  else if (changePercent > 0) scoreChange = 20
  else if (changePercent > -2) scoreChange = 10
  details.push({ label: `涨幅(${changePercent > 0 ? '+' : ''}${changePercent.toFixed(2)}%)`, score: scoreChange, max: 20 })

  let scoreWeibi = 2
  if (weibi >= 99 && outerRatio < 60) scoreWeibi = 0
  else if (weibi > 30) scoreWeibi = 10
  else if (weibi > 0) scoreWeibi = 7
  else if (weibi > -30) scoreWeibi = 4
  details.push({ label: `委比(${weibi > 0 ? '+' : ''}${weibi.toFixed(2)}%)`, score: scoreWeibi, max: 10 })

  let scoreAvg = 2
  if (avgPriceDeviation > 1.5) scoreAvg = 5
  else if (avgPriceDeviation > 0) scoreAvg = 15
  else if (avgPriceDeviation > -1) scoreAvg = 8
  details.push({ label: `均价偏离(${avgPriceDeviation > 0 ? '+' : ''}${avgPriceDeviation.toFixed(2)}%)`, score: scoreAvg, max: 15 })

  let scoreAmplitude = 2
  if (amplitude > 2 && amplitude <= 6) scoreAmplitude = 5
  else if (amplitude > 1 && amplitude <= 2) scoreAmplitude = 3
  details.push({ label: `振幅(${amplitude.toFixed(2)}%)`, score: scoreAmplitude, max: 5 })

  let scorePosition = 5
  if (pe < 0) scorePosition = 2
  details.push({ label: '基本面修正', score: scorePosition, max: 5 })

  const totalScore = scoreOuter + scoreVolume + scoreTurnover + scoreChange + scoreWeibi + scoreAvg + scoreAmplitude + scorePosition
  const normalized = Math.min(100, Math.round(totalScore))

  let label = ''
  if (normalized >= 80) label = '强烈看涨'
  else if (normalized >= 70) label = '建议关注'
  else if (normalized >= 55) label = '中性观望'
  else if (normalized >= 40) label = '谨慎回调'
  else label = '强烈避险'

  return { score: normalized, label, details }
}

// 统一评级：标签 + 颜色（前后端共用同一份阈值）
export function getRating(score) {
  if (score >= 80) return { label: '强烈看涨', short: '看涨', color: 'var(--red)' }
  if (score >= 70) return { label: '建议关注', short: '看涨', color: 'var(--red)' }
  if (score >= 55) return { label: '中性观望', short: '观望', color: 'var(--orange)' }
  if (score >= 40) return { label: '谨慎回调', short: '回调', color: 'var(--orange)' }
  return { label: '强烈避险', short: '看跌', color: 'var(--green)' }
}

function getPlateRelation(data) {
  const outer = data.outerPlateRaw || 0
  const inner = data.innerPlateRaw || 0
  if (outer === 0 && inner === 0) return 'none'
  if (outer === 0) return 'innerOnly'
  if (inner === 0) return 'outerOnly'
  const ratio = inner / outer
  if (ratio >= 1.3) return 'innerStrong'
  if (ratio <= 1 / 1.3) return 'outerStrong'
  if (ratio >= 0.9 && ratio <= 1.1) return 'balanced'
  if (ratio > 1.1) return 'slightInner'
  return 'slightOuter'
}

function getVolumeLevel(data) {
  const vr = data.volumeRatio || 0
  if (vr >= 1.5) return 'expansion'
  if (vr < 0.5) return 'shrinkage'
  if (vr >= 1) return 'slightExpand'
  return 'slightShrink'
}

function getPositionLevel(data) {
  const cp = Math.abs(data.changePercent || 0)
  const pe = data.pe || 0
  if (cp > 8 || (pe > 0 && pe > 500)) return '高位'
  if (cp > 3 || (pe > 0 && pe > 200)) return '中位'
  if (cp < 1 && pe > 0 && pe < 50) return '低位'
  return '中位'
}

function getPriceVsAvgLine(data) {
  const price = data.priceRaw || parseFloat(data.price)
  const avg = data.avgPrice || 0
  if (avg <= 0) return 'unknown'
  return price >= avg ? 'above' : 'below'
}

function isDisabledScenario(data) {
  const price = data.priceRaw || parseFloat(data.price)
  const highLimit = data.highLimit || 0
  const lowLimit = data.lowLimit || 0
  const volume = data.volumeRaw || 0
  const outerPlate = data.outerPlateRaw || 0
  const innerPlate = data.innerPlateRaw || 0
  if (highLimit > 0 && price >= highLimit * 0.999) return { reason: '涨停板附近，内外盘失真失效' }
  if (lowLimit > 0 && price <= lowLimit * 1.001) return { reason: '跌停板附近，内外盘失真失效' }
  if (volume === 0 || (outerPlate === 0 && innerPlate === 0)) return { reason: '无有效成交数据，策略不可用' }
  return null
}

export function getEnhancedAnalysis(data) {
  const disabled = isDisabledScenario(data)
  if (disabled) return { disabled: true, reason: disabled.reason }

  const plateRel = getPlateRelation(data)
  const volLevel = getVolumeLevel(data)
  const position = getPositionLevel(data)
  const priceVsAvg = getPriceVsAvgLine(data)
  const change = data.change || 0
  const dayUp = change > 0
  const dayDown = change < 0
  const changePercent = data.changePercent || 0
  const volumeRatio = data.volumeRatio || 0

  const volLabel = volLevel === 'expansion' ? '放量' : volLevel === 'shrinkage' ? '缩量' : '平量'
  const volDesc = `量比${volumeRatio.toFixed(2)}·${volLabel}`

  if ((plateRel === 'innerStrong' || plateRel === 'slightInner') && dayUp) {
    if (position === '高位') {
      return { type: 'bearish', title: '⚠️ 高位疑为诱多', desc: '内盘>外盘但股价上涨，位置在高位', reason: '高位吸筹不成立，极大可能是主力诱多派发', position, volumeDesc: volDesc, action: '不建议参与，已有仓位减仓止盈' }
    }
    const isShrinkOrNormal = volLevel === 'shrinkage' || volLevel === 'slightShrink'
    if (priceVsAvg === 'above' && isShrinkOrNormal) {
      return { type: 'bullish', title: '📈 模型1：低位暗吸筹（最优）', desc: '内盘>外盘·缩量/平量上涨·分时站均线上', reason: '主动卖单多但股价抗跌上涨，分时均价线上方·缩量无出货·拆单特征不明显', position, volumeDesc: volDesc, action: '持股锁仓不动，不做T不轻易下车；低位可分批加仓' }
    }
    return { type: 'bullish', title: '📈 模型1：主力暗吸筹', desc: '内盘>外盘但股价上涨', reason: '主动卖单多但股价反而上涨，主力小单砸、托单低吸', position, volumeDesc: volDesc, action: '持股不动，观察量能和分时均价变化' }
  }

  if ((plateRel === 'innerStrong' || plateRel === 'slightInner') && dayDown) {
    if (volLevel !== 'expansion' && volLevel !== 'slightExpand') {
      return { type: 'bearish', title: '📉 模型2：内盘大但缩量阴跌', desc: '内盘>外盘下跌但未放量', reason: '阴跌缩量不算主卖出逃，可能是弱势盘整', position, volumeDesc: volDesc, action: '观望为主，若持续缩量下行则减仓' }
    }
    if (priceVsAvg === 'below') {
      const strength = position === '高位' ? '（最强离场信号）' : ''
      return { type: 'bearish', title: '📉 模型2：主力坚决出逃' + strength, desc: '内盘>外盘·放量下跌·分时均线下', reason: '主动卖单蜂拥砸盘，分时始终在均价线下方，反抽无力，放量确认', position, volumeDesc: volDesc, action: '不分盈亏果断离场，不抄底不补仓！全位置通用，高位最强' }
    }
    return { type: 'bearish', title: '📉 模型2：主力出逃信号', desc: '内盘>外盘·放量下跌', reason: '主动卖单放量砸盘，资金集体跑路', position, volumeDesc: volDesc, action: '考虑减仓或离场，观察分时均价线' }
  }

  if ((plateRel === 'outerStrong' || plateRel === 'slightOuter') && dayDown) {
    if (priceVsAvg === 'below') {
      return { type: 'bearish', title: '📉 模型3：典型诱多出货', desc: '外盘>内盘·股价下跌·分时均线下', reason: '外盘大但股价站不上昨收、分时均价线下方，大买单托盘制造抢筹假象，真实卖单拆小单悄悄砸', position, volumeDesc: volDesc, action: '坚决不抄底不接盘！任何位置都只卖不买，高位中位尤其危险' }
    }
    return { type: 'bearish', title: '📉 模型3：疑似诱多出货', desc: '外盘>内盘但股价下跌', reason: '主动买单多但价格一直在昨收下方震荡，需留意大买单托盘、小卖单出货的拆单手法', position, volumeDesc: volDesc, action: '不抄底不追入，观察分时均价线确认方向' }
  }

  if ((plateRel === 'outerStrong' || plateRel === 'slightOuter') && dayUp) {
    if (position === '高位' && volLevel === 'expansion') {
      return { type: 'watch', title: '⚡ 模型4变种：高位放量追涨', desc: '外盘>内盘·涨·高位爆量', reason: '高位急涨+爆量要防次日分歧和最后一波诱多', position, volumeDesc: volDesc, action: '高位只止盈不追高，已有仓位可分批卖出锁定利润' }
    }
    if (priceVsAvg === 'above') {
      return { type: 'bullish', title: '📈 模型4：量价齐升多头强势', desc: '外盘>内盘·上涨·分时均线上', reason: '主动买入旺盛配合成交量，股价稳步站上昨收+分时均价线上方，真实抢筹', position, volumeDesc: volDesc, action: position === '高位' ? '高位只止盈不追高' : '中低位持股不动，可小仓位加仓' }
    }
    return { type: 'bullish', title: '📈 模型4：多头看涨', desc: '外盘>内盘且今日上涨', reason: '主动买单多+股价涨，多头占优', position, volumeDesc: volDesc, action: '持有观察，注意分时均价线方向' }
  }

  if (volLevel === 'shrinkage' && data.outerPlateRaw + data.innerPlateRaw < 5000000) {
    if (Math.abs(changePercent) < 1.5) {
      return { type: 'hold', title: '🔒 模型5：筹码锁仓洗盘', desc: '内外盘极小·缩量明显·窄幅波动', reason: '主动买卖都极少，没人砸也没人抢，主力锁仓洗盘等风来', position, volumeDesc: volDesc, action: '不交易不折腾，耐心等突破信号，不用频繁做T；底部横盘最佳' }
    }
    return { type: 'hold', title: '🔒 模型5：筹码锁仓', desc: '成交缩量低迷', reason: '主动买卖盘极小，主力锁仓中', position, volumeDesc: volDesc, action: '耐心持有，等待放量选方向' }
  }

  if (plateRel === 'balanced' && volLevel === 'expansion') {
    return { type: 'watch', title: '⚡ 模型6：多空博弈即将变盘', desc: '内外盘均衡·明显放量', reason: '主动买卖力量相当但突然放量，箱体末端或关键支撑/压力位选择方向', position, volumeDesc: volDesc, action: '纯观望不提前布局！等向上突破再进、向下跌破再离场，不做预判' }
  }

  const trend = dayUp ? '偏多' : dayDown ? '偏空' : '平盘'
  return { type: 'hold', title: '🔍 综合信号不明确', desc: `当前盘面${trend}·${position}·${volDesc}`, reason: '内外盘未见显著强弱差距，量能方向不明确，建议观望', position, volumeDesc: volDesc, action: '轻仓观望，等待明确信号出现再操作' }
}
