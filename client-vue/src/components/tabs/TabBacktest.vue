<template>
  <div class="bt">
    <div class="bt-topbar">
      <div>
        <h2 class="bt-title">回测曲线</h2>
        <p class="bt-sub">把历史推荐按买卖口径重放一遍：{{ meta }}</p>
      </div>
      <div class="bt-range">
        <button v-for="m in [3, 6, 12]" :key="m" class="bt-seg" :class="{ on: months === m }" @click="setMonths(m)">
          近{{ m }}个月
        </button>
      </div>
    </div>

    <div v-if="loading" class="bt-loading">正在按六种口径重放历史推荐…（首次约 1 秒，之后半小时走缓存）</div>
    <div v-else-if="error" class="bt-error">{{ error }}</div>

    <template v-else>
      <div class="bt-kpis">
        <div class="bt-kpi">
          <span class="bt-kpi-k">本金</span>
          <span class="bt-kpi-v">{{ fmt(capital) }}</span>
        </div>
        <div class="bt-kpi">
          <span class="bt-kpi-k">按推荐执行 · 次日开盘卖</span>
          <span class="bt-kpi-v" :class="signCls(head.retPct)">{{ fmt(head.end) }}</span>
          <span class="bt-kpi-x" :class="signCls(head.retPct)">{{ pct(head.retPct) }}</span>
        </div>
        <div class="bt-kpi">
          <span class="bt-kpi-k">按推荐执行 · 次日尾盘卖</span>
          <span class="bt-kpi-v" :class="signCls(head2.retPct)">{{ fmt(head2.end) }}</span>
          <span class="bt-kpi-x" :class="signCls(head2.retPct)">{{ pct(head2.retPct) }}</span>
        </div>
        <div class="bt-kpi">
          <span class="bt-kpi-k">同期等权背景</span>
          <span v-if="bench" class="bt-kpi-v" :class="signCls(bench.avgPct)">{{ pct(bench.avgPct) }}</span>
          <button v-else class="bt-mini" @click="loadBench">算一下</button>
        </div>
      </div>

      <section class="bt-card">
        <header class="bt-card-h">
          <h3>资金曲线</h3>
          <div class="bt-legend">
            <button
              v-for="c in curves"
              :key="c.key"
              class="bt-leg"
              :class="{ on: shown.has(c.key) }"
              @click="toggle(c.key)"
            >
              <i class="bt-swatch" :style="{ borderColor: hueOf(c), borderStyle: lineStyle(c) }"></i>{{ short(c) }}
            </button>
          </div>
        </header>
        <div v-if="batchNote" class="bt-note">{{ batchNote }}</div>

        <svg
          class="bt-chart"
          :viewBox="`0 0 ${W} ${H}`"
          preserveAspectRatio="none"
          @click="onTap"
          @touchstart.passive="onTap"
        >
          <line v-for="g in yGrids" :key="'g' + g.v" :x1="PAD_L" :x2="W - PAD_R" :y1="g.y" :y2="g.y" class="bt-grid" />
          <line :x1="PAD_L" :x2="W - PAD_R" :y1="yOf(capital)" :y2="yOf(capital)" class="bt-zero" />
          <text v-for="g in yGrids" :key="'t' + g.v" :x="PAD_L - 6" :y="g.y + 3" class="bt-ax">{{ g.label }}</text>
          <text v-for="t in xTicks" :key="'x' + t.i" :x="t.x" :y="H - 6" class="bt-ax bt-ax-x">{{ t.label }}</text>

          <polyline
            v-for="c in visible"
            :key="c.key"
            :points="path(c)"
            class="bt-line"
            :stroke="hueOf(c)"
            :stroke-width="c.default ? 2.4 : 1.5"
            :stroke-dasharray="dashOf(c)"
            :opacity="c.default ? 1 : 0.8"
          />
          <g v-if="readIdx !== null">
            <line :x1="xOf(readIdx)" :x2="xOf(readIdx)" :y1="PAD_T" :y2="H - PAD_B" class="bt-cursor" />
            <circle
              v-for="c in visible"
              :key="'p' + c.key"
              :cx="xOf(readIdx)"
              :cy="yOf(navAt(c, readIdx))"
              r="3"
              :fill="hueOf(c)"
            />
          </g>
        </svg>

        <div class="bt-read">
          <template v-if="readRow">
            <span class="bt-read-d">{{ readRow.date }}</span>
            <span v-for="c in visible" :key="'r' + c.key" class="bt-read-i">
              <i class="bt-swatch" :style="{ borderColor: hueOf(c), borderStyle: lineStyle(c) }"></i>{{ short(c) }}
              <b :class="signCls(navAt(c, readIdx) - capital)">{{ fmt(navAt(c, readIdx)) }}</b>
            </span>
            <button class="bt-mini" @click="readIdx = null">清除</button>
          </template>
          <span v-else class="bt-read-hint"
            >在图上点一下可以看任意一天的余额。颜色＝卖法（青＝次日开盘卖，橙＝次日尾盘卖），
            线型＝入口（实线＝按推荐执行，虚线＝尾盘买不到就剔除，点线＝排板全成交的上界，现实做不到）。</span
          >
        </div>

        <div class="bt-daily">
          <div class="bt-daily-h">
            <span>每日监控点</span>
            <em>{{
              daily.length
                ? `已攒 ${daily.length} 天（16:20 落一次，窗口固定回看 3 个月）`
                : '还没跑过：定时任务 16:20 落第一个点（周末不跑）'
            }}</em>
          </div>
          <table v-if="daily.length" class="bt-daily-t">
            <thead>
              <tr>
                <th>日期</th>
                <th>按推荐执行·开盘卖</th>
                <th>尾盘硬吃·开盘卖</th>
                <th>入口差</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="d in daily.slice(-7).reverse()" :key="d.date">
                <td>{{ d.date.slice(5) }}</td>
                <td :class="signCls(d.auction)">{{ d.auction === null ? '—' : pct(d.auction) }}</td>
                <td :class="signCls(d.tailclose)">{{ d.tailclose === null ? '—' : pct(d.tailclose) }}</td>
                <td v-if="d.auction !== null && d.tailclose !== null" :class="signCls(d.auction - d.tailclose)">
                  {{ pct(d.auction - d.tailclose) }}
                </td>
                <td v-else>—</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="bt-fine">
            为什么要每天存一个点而不是每次现算：现算用的是"今天为止的全部推荐"，窗口天天在动，
            前两天的数字放在一起根本不可比。所有"放宽过滤 / 多推 / 改模式"的提案，
            验收标准都写成这一列（按推荐执行口径不恶化），没有这条序列就没有判据。
          </p>
        </div>
      </section>

      <section class="bt-card">
        <header class="bt-card-h">
          <h3>推荐的真实胜率</h3>
          <span class="bt-note">{{ verdict }}</span>
        </header>
        <div v-if="!trend.length" class="bt-error">胜率走势暂无数据（复盘任务每天 16:10 跑，周末不更新）。</div>
        <template v-else>
          <svg class="bt-chart bt-chart-2" :viewBox="`0 0 ${W} ${H2}`" preserveAspectRatio="none">
            <line
              v-for="g in yGrids2"
              :key="'w' + g.v"
              :x1="PAD_L"
              :x2="W - PAD_R"
              :y1="g.y"
              :y2="g.y"
              class="bt-grid"
            />
            <text v-for="g in yGrids2" :key="'wt' + g.v" :x="PAD_L - 6" :y="g.y + 3" class="bt-ax">{{ g.label }}</text>
            <line :x1="PAD_L" :x2="W - PAD_R" :y1="yOf2(50)" :y2="yOf2(50)" class="bt-zero" />
            <polyline
              v-if="lineLow.length > 1"
              :points="lineLow"
              class="bt-line"
              stroke="var(--accent-info)"
              stroke-width="1.2"
              opacity=".65"
            />
            <polyline
              v-if="lineChase.length > 1"
              :points="lineChase"
              class="bt-line"
              stroke="var(--accent-secondary)"
              stroke-width="1.2"
              opacity=".65"
            />
            <polyline
              v-if="rollLow.length > 1"
              :points="rollLow"
              class="bt-line"
              stroke="var(--accent-info)"
              stroke-width="2.4"
            />
            <polyline
              v-if="rollChase.length > 1"
              :points="rollChase"
              class="bt-line"
              stroke="var(--accent-secondary)"
              stroke-width="2.4"
            />
            <text :x="W - PAD_R" :y="PAD_T + 12" class="bt-ax bt-ax-r">浅线＝逐日，粗线＝近5个交易日滚动</text>
          </svg>
          <div class="bt-legend bt-legend-2">
            <span class="bt-leg on"
              ><i class="bt-swatch" style="border-color: var(--accent-info)"></i>低吸参考价成交
              {{ aggTotal('t1') }}</span
            >
            <span class="bt-leg on"
              ><i class="bt-swatch" style="border-color: var(--accent-secondary)"></i>现价追入成交
              {{ aggTotal('exec_t1') }}</span
            >
          </div>
        </template>
        <div v-if="modeRows.length" class="bt-modes">
          <div class="bt-daily-h"><span>模式胜率榜</span><em>标签从 10-19 的批次开始才有，此前记录都归"其他"</em></div>
          <table class="bt-daily-t">
            <thead>
              <tr>
                <th>模式</th>
                <th>条数</th>
                <th>T+1 胜率</th>
                <th>T+1 均值</th>
                <th>T+3 胜率</th>
                <th>吃大面</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="m in modeRows" :key="m.mode">
                <td>{{ m.mode }}</td>
                <td>{{ m.n }}</td>
                <td :class="signCls((m.t1 || {}).win_rate - 50)">{{ fmtRate(m.t1) }}</td>
                <td :class="signCls((m.t1 || {}).avg)">{{ fmtAvg(m.t1) }}</td>
                <td>{{ fmtRate(m.t3) }}</td>
                <td>{{ m.noodle_count != null ? m.noodle_count + ' 条' : '—' }}</td>
              </tr>
            </tbody>
          </table>
        </div>
        <p class="bt-fine">{{ trendCaveat }}</p>
      </section>

      <section v-if="sh.rows.length || sh.note" class="bt-card">
        <details class="bt-det">
          <summary>影子规则对照（第1批「周期门槛线上化」的上线判据）· {{ shVerdict }}</summary>
          <p class="bt-fine">
            每天的推荐任务都会用"周期门槛 + 角色否决 + 不追高"这三条规则<b>另选一遍但不下单</b>，
            这里比的是<b>被它挡掉的票</b>和<b>它放行的票</b>第二天谁涨得好。
            挡掉的确实更差 → 值得搬到线上；反过来更好 → 这套规则在削收益，不能上。
            判定要攒满 {{ sh.minDays || 10 }} 个<b>有真放行名单</b>的交易日（名单是 10-18 才开始整份存的），
            不够就只报数不给结论。
          </p>
          <table class="bt-daily-t">
            <thead>
              <tr>
                <th>运行日</th>
                <th>真名单天数</th>
                <th>放行 T+1</th>
                <th>被挡 T+1</th>
                <th>均值差</th>
                <th>判定</th>
              </tr>
            </thead>
            <tbody>
              <tr v-if="!sh.rows.length">
                <td colspan="6" class="bt-name">{{ sh.note || '还没有监控点' }}</td>
              </tr>
              <tr v-for="r in sh.rows" :key="r.eval_date">
                <td>{{ r.eval_date.slice(5) }}</td>
                <td>{{ r.real_days != null ? r.real_days + ' 天' : '—' }}</td>
                <td>{{ shCell(r.kept_n, r.kept_win, r.kept_avg) }}</td>
                <td>{{ shCell(r.excl_n, r.excl_win, r.excl_avg) }}</td>
                <td :class="signCls(r.gap_avg)">{{ r.gap_avg == null ? '—' : pct(r.gap_avg) }}</td>
                <td class="bt-name">{{ r.verdict }}</td>
              </tr>
            </tbody>
          </table>
          <p class="bt-fine">
            口径：当日复权收盘 → 次日复权收盘（两组同成本基准才可比）。
            实现在 <code>server/scripts/shadow_eval.py</code>，命令行跑
            <code>python scripts/shadow_eval.py</code> 得到同一批数。
          </p>
        </details>
      </section>

      <section class="bt-card">
        <details class="bt-det">
          <summary>六种口径的数字与"为什么这些数不能当承诺"</summary>
          <table class="bt-table">
            <thead>
              <tr>
                <th>口径</th>
                <th>期末</th>
                <th>收益</th>
                <th>回撤</th>
                <th>成交/跳过</th>
                <th>胜率</th>
                <th>单笔均(加权)</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="c in curves" :key="'tb' + c.key" :class="{ dim: !shown.has(c.key) }">
                <td class="bt-name">
                  <i class="bt-swatch" :style="{ borderColor: hueOf(c), borderStyle: lineStyle(c) }"></i>{{ c.label }}
                </td>
                <td>{{ fmt(c.end) }}</td>
                <td :class="signCls(c.retPct)">{{ pct(c.retPct) }}</td>
                <td>{{ c.mdd.toFixed(1) }}%</td>
                <td>{{ c.trades }} / {{ c.skipped }}</td>
                <td>{{ c.winRate }}%</td>
                <td :class="signCls(c.avgWeighted)">{{ pct(c.avgWeighted) }}</td>
              </tr>
            </tbody>
          </table>
          <div class="bt-why">
            <p><b>三条会骗人的地方，这里都按"现实里做不到就不算"处理：</b></p>
            <ul>
              <li>
                尾盘已经封死涨停的票<b>买不到</b>。系统偏爱强势票，{{ metaRec }} 条里有 {{ sealedCount }} 条当天封死；
                把这些当成"我买到了"，三个月能从 <b>{{ pct(byKey('tailclose_open').retPct) }}</b> 变成
                <b>{{ pct(byKey('filled_open').retPct) }}</b> —— 这种"改进"是假的。
              </li>
              <li>次日一字跌停<b>卖不掉</b>，曲线里顺延到下一根K线才成交（一鸣食品 09-01 那种连吃两个一字）。</li>
              <li>
                日K是前复权、推荐价是当时实盘价，期间除过权就会<b>两个尺度对不上</b>，
                这类行的买入价退回当日复权收盘并单独计数。
              </li>
            </ul>
            <p class="bt-fine">
              分仓＝当日可用现金按推荐只数均分、100 股一手（高价票因此自动少买，这是真实约束）；
              费用＝佣金万2.5（最低5元）+ 印花税千1（卖）+ 过户万0.1。
              曲线只统计<b>系统真的推过的票</b>，当天没推的看不见； 样本 {{ metaDays }} 个推荐日 /
              {{ metaRec }} 条，几十笔的量级上 ±5 个点都在噪声里， 别把任何一个期末数字当成预期。
            </p>
            <p class="bt-fine">
              数据时间 {{ generatedAt }}；同一套实现在 <code>server/services/pick/rec_sim.py</code>，命令行跑
              <code>python scripts/rec_exit_sim.py</code> 得到的是同一批数。
            </p>
          </div>
        </details>
      </section>
    </template>
  </div>
</template>

<script setup>
  import { ref, computed, onMounted } from 'vue'
  import { apiFetch } from '../../utils/helpers'
  import { domainOf, xAt, yAt, polyline, gridValues, rollingAvg, weightedRate } from '../../utils/chart'

  const capital = 100000
  const months = ref(3)
  const loading = ref(true)
  const error = ref('')
  const data = ref(null)
  const trend = ref([])
  const modeBoard = ref({})
  const sh = ref({ rows: [], note: '', minDays: 10 })
  const readIdx = ref(null)

  const W = 760,
    H = 230,
    H2 = 160,
    PAD_L = 54,
    PAD_R = 10,
    PAD_T = 12,
    PAD_B = 22
  // 颜色只表达"卖法"，线型表达"入口"。A股是红涨绿跌，所以红/绿这两个语义色
  // 一律留给数字本身（赚了红、亏了绿），曲线不碰它们，免得看图的人在颜色上读反。
  const HUE = { open: 'var(--accent-info)', close: 'var(--accent-warning)' }
  // am/pm＝上午档/下午档拆批次线（2026-10-21）：虚线区分末批实线，颜色仍按卖法
  const DASH = { auction: null, tailclose: '6 3', filled: '2 3', am: '9 3', pm: '2 3' }
  const linestyleOf = c =>
    c.entry === 'tailclose' || c.entry === 'am'
      ? 'dashed'
      : c.entry === 'filled' || c.entry === 'pm'
        ? 'dotted'
        : 'solid'

  const curves = computed(() => (data.value && data.value.curves) || [])
  const batchNote = ref('')
  const shown = ref(new Set())
  const visible = computed(() => curves.value.filter(c => shown.value.has(c.key)))
  const byKey = k => curves.value.find(c => c.key === k) || { retPct: 0, end: capital, nav: [] }
  const head = computed(() => byKey('auction_open'))
  const head2 = computed(() => byKey('auction_close'))
  const bench = computed(() =>
    data.value && data.value.benchmark && data.value.benchmark.available ? data.value.benchmark : null
  )
  const generatedAt = computed(() => (data.value && data.value.generatedAt) || '')
  const w = computed(() => (data.value && data.value.window) || {})
  const metaDays = computed(() => w.value.recDays || 0)
  const metaRec = computed(() => w.value.recCount || 0)
  const meta = computed(
    () => `${w.value.since || '—'} → ${w.value.until || '—'}，${metaDays.value} 个推荐日 / ${metaRec.value} 条推荐`
  )
  // 14:45 快照里已经封死的推荐条数：这个数字决定"尾盘买入"那一档有多少票根本买不到
  const sealedCount = computed(() => {
    const s = byKey('tailclose_open').skipReasons || {}
    return Object.entries(s)
      .filter(([k]) => k.includes('封死'))
      .reduce((a, [, v]) => a + v, 0)
  })

  // 每日监控点：16:20 定时任务落的 rec_sim_daily，按日期归并成"三种入口各一列"
  const daily = computed(() => {
    const by = {}
    ;(data.value.daily || []).forEach(r => {
      const d = (by[r.run_date] = by[r.run_date] || { date: r.run_date, auction: null, tailclose: null })
      if (r.mode === 'open' && r.entry === 'auction') d.auction = r.ret_pct
      if (r.mode === 'open' && r.entry === 'tailclose') d.tailclose = r.ret_pct
    })
    return Object.values(by).sort((a, b) => (a.date < b.date ? -1 : 1))
  })

  // 模式榜：by_mode 的键是中文模式名，值就是 _aggregate 的结构（t1/t3/exec_t1/noodle_count）
  const modeRows = computed(() =>
    Object.entries(modeBoard.value || {})
      .filter(([, v]) => v && v.t1)
      .map(([mode, v]) => ({ mode, ...v }))
  )
  const fmtRate = o => (o && o.win_rate != null ? `${o.win_rate}%（${o.n} 条）` : '—')
  const fmtAvg = o => (o && o.avg != null ? `${o.avg > 0 ? '+' : ''}${o.avg}%` : '—')

  function toggle(k) {
    const n = new Set(shown.value)
    if (n.has(k)) {
      n.delete(k)
    } else {
      n.add(k)
    }
    shown.value = n
  }
  const hueOf = c => HUE[c.mode] || 'var(--text-tertiary)'
  const dashOf = c => DASH[c.entry]
  const lineStyle = linestyleOf
  const short = c =>
    (c.sideLabel ? `${c.sideLabel}·${c.modeLabel}` : `${c.entryLabel.split('（')[0]}·${c.modeLabel}`)

  /* ---------- 资金曲线（坐标换算全在 utils/chart.js，那边有 Node 断言） ---------- */
  const BOX = { w: W, h: H, pl: PAD_L, pr: PAD_R, pt: PAD_T, pb: PAD_B }
  const BOX2 = { w: W, h: H2, pl: PAD_L, pr: PAD_R, pt: PAD_T, pb: PAD_B }
  const allDates = computed(() => {
    const set = new Set()
    curves.value.forEach(c => (c.nav || []).forEach(p => set.add(p.date)))
    return [...set].sort()
  })
  const navAt = (c, i) => {
    const d = allDates.value[i]
    const hit = (c.nav || []).find(p => p.date === d)
    if (hit) return hit.nav
    const prev = [...(c.nav || [])].reverse().find(p => p.date <= d)
    return prev ? prev.nav : capital // 这条线还没开始买 = 现金原样
  }
  const seriesOf = c => allDates.value.map((_d, i) => navAt(c, i))
  const dom = computed(() => domainOf(visible.value.map(seriesOf), capital))
  const xOf = i => xAt(i, allDates.value.length, BOX)
  const yOf = v => yAt(v, dom.value[0], dom.value[1], BOX)
  const path = c => polyline(seriesOf(c), dom.value[0], dom.value[1], BOX)
  const yGrids = computed(() =>
    gridValues(dom.value[0], dom.value[1], 3).map(v => ({ v, y: yOf(v), label: (v / 10000).toFixed(1) + '万' }))
  )
  const xTicks = computed(() => {
    const n = allDates.value.length
    if (!n) return []
    const step = Math.max(1, Math.floor((n - 1) / 5))
    const out = []
    for (let i = 0; i < n; i += step) out.push({ i, x: xOf(i), label: allDates.value[i].slice(5) })
    if (out[out.length - 1].i !== n - 1) out.push({ i: n - 1, x: xOf(n - 1), label: allDates.value[n - 1].slice(5) })
    return out
  })
  const readRow = computed(() => (readIdx.value === null ? null : { date: allDates.value[readIdx.value] }))

  function onTap(ev) {
    const box = ev.currentTarget.getBoundingClientRect()
    const px = (((ev.touches ? ev.touches[0].clientX : ev.clientX) - box.left) / box.width) * W
    const n = allDates.value.length
    if (!n) return
    const t = Math.min(1, Math.max(0, (px - PAD_L) / (W - PAD_L - PAD_R)))
    readIdx.value = Math.round(t * (n - 1))
  }

  /* ---------- 胜率走势：0~100 固定量程，两条口径同轴才可比 ---------- */
  const yOf2 = v => yAt(v, 0, 100, BOX2)
  const yGrids2 = computed(() => [0, 25, 50, 75, 100].map(v => ({ v, y: yOf2(v), label: v + '%' })))
  const pairsOf = f => trend.value.map(d => ({ v: d[f], n: d.n, avg: d.avg }))
  const lineLow = computed(() =>
    polyline(
      trend.value.map(d => d.t1),
      0,
      100,
      BOX2
    )
  )
  const lineChase = computed(() =>
    polyline(
      trend.value.map(d => d.exec),
      0,
      100,
      BOX2
    )
  )
  const rollLow = computed(() => polyline(rollingAvg(pairsOf('t1'), 5), 0, 100, BOX2))
  const rollChase = computed(() => polyline(rollingAvg(pairsOf('exec'), 5), 0, 100, BOX2))
  function aggTotal(field) {
    const r = weightedRate(pairsOf(field))
    if (!r) return '无数据'
    return `胜率 ${r.win.toFixed(1)}%（${r.n} 条）·均值 ${r.avg.toFixed(2)}%`
  }
  const verdict = computed(() => {
    const half = Math.floor(trend.value.length / 2)
    if (half < 3) return ''
    const a = weightedRate(pairsOf('exec').slice(0, half))
    const b = weightedRate(pairsOf('exec').slice(half))
    if (!a || !b) return ''
    const d = b.win - a.win
    return (
      `现价追入口径：前半段 ${a.win.toFixed(1)}% → 后半段 ${b.win.toFixed(1)}%` +
      `（${d >= 0 ? '+' : ''}${d.toFixed(1)} 个点，前 ${a.n} 条 / 后 ${b.n} 条）`
    )
  })
  const trendCaveat =
    '两条线是两种成交假设：蓝＝按卡片低吸参考价挂到才买上，红＝看到推荐就用现价追。' +
    '历史推荐是在旧口径下产出的（10-16 之前允许 ST/创业板，评分权重每周还会自动优化），' +
    '所以这条走势里混着"行情变了""口径变了""权重变了"三件事，不能读成"策略在变好"。'

  /* ---------- 载入 ---------- */
  const fmt = v => Math.round(v || 0).toLocaleString('zh-CN')
  const pct = v => `${v >= 0 ? '+' : ''}${(v || 0).toFixed(2)}%`
  const signCls = v => (v > 0 ? 'gain' : v < 0 ? 'loss' : '')

  // 影子对照：一行 "n次 胜率/均值"；n=0 或 NULL 都写"无成熟样本"而不是 0%（0% 会被读成"全都亏"）
  function shCell(n, win, avg) {
    if (!n) return '无成熟样本'
    return `${n}次 ${win}%/${avg >= 0 ? '+' : ''}${avg}%`
  }
  const shVerdict = computed(() => {
    const rows = sh.value.rows || []
    if (!rows.length) return sh.value.note || '还没开始攒'
    const r = rows[0]
    const need = sh.value.minDays || 10
    const have = r.real_days == null ? 0 : r.real_days
    return `${r.verdict}（真名单 ${have}/${need} 天${have >= need ? '' : '，还在攒'}）`
  })

  async function loadSim(withBench) {
    loading.value = true
    error.value = ''
    try {
      const d = await apiFetch(`/api/recommend-sim?months=${months.value}&bench=${withBench ? 1 : 0}`)
      data.value = d.data
      // 拆批次曲线（上午档/下午档，快照 10-19 起）并进同一组图例；失败不影响主曲线
      try {
        const bd = await apiFetch(`/api/recommend-sim-batch?months=${months.value}`)
        const bc = (bd.data && bd.data.curves) || []
        bc.forEach(c => {
          c.entry = c.side
        })
        data.value.curves = (data.value.curves || []).concat(bc)
        batchNote.value = (bd.data && bd.data.note) || ''
      } catch (e) {
        batchNote.value = ''
      }
      shown.value = new Set((data.value.curves || []).filter(c => c.default).map(c => c.key))
      readIdx.value = null
    } catch (e) {
      error.value = '回测曲线加载失败：' + (e.message || e)
    } finally {
      loading.value = false
    }
  }
  async function loadBench() {
    await loadSim(true)
  }
  function setMonths(m) {
    months.value = m
    loadSim(!!bench.value)
  }

  onMounted(async () => {
    loadSim(false)
    try {
      const d = await apiFetch('/api/recommend-review?days=120')
      const by = (d.data && d.data.by_date) || {}
      trend.value = Object.entries(by)
        .sort((a, b) => (a[0] < b[0] ? -1 : 1))
        .map(([date, v]) => ({
          date,
          n: (v.t1 && v.t1.n) || 0,
          t1: v.t1 ? v.t1.win_rate : null,
          exec: v.exec_t1 ? v.exec_t1.win_rate : null,
          avg: (v.exec_t1 && v.exec_t1.avg) ?? (v.t1 && v.t1.avg) ?? 0,
        }))
        .filter(r => r.n > 0)
      modeBoard.value = (d.data && d.data.by_mode) || {}
    } catch (e) {
      /* 胜率走势拿不到不影响曲线 */
    }
    try {
      const s = await apiFetch('/api/shadow-eval?limit=20')
      sh.value = {
        rows: (s.data && s.data.rows) || [],
        note: (s.data && s.data.note) || '',
        minDays: (s.data && s.data.minDays) || 10,
      }
    } catch (e) {
      /* 影子对照是监控，读不到就整块不显示 */
    }
  })
</script>

<style scoped>
  .bt {
    padding: var(--spacing-sm);
  }
  .bt-topbar {
    display: flex;
    justify-content: space-between;
    align-items: flex-start;
    gap: 8px;
    flex-wrap: wrap;
  }
  .bt-title {
    margin: 0;
    font-size: 15px;
    color: var(--text-primary);
  }
  .bt-sub {
    margin: 3px 0 0;
    font-size: 11px;
    color: var(--text-tertiary);
  }
  .bt-range {
    display: flex;
    gap: 4px;
  }
  .bt-seg {
    padding: 4px 9px;
    font-size: 11px;
    border-radius: var(--radius-pill);
    cursor: pointer;
    border: 1px solid var(--border-subtle);
    background: var(--bg-secondary);
    color: var(--text-secondary);
  }
  .bt-seg.on {
    border-color: var(--accent-primary);
    color: var(--text-primary);
    background: var(--bg-elevated);
  }
  .bt-loading,
  .bt-error {
    padding: var(--spacing-lg);
    text-align: center;
    font-size: 12px;
    color: var(--text-tertiary);
    background: var(--bg-secondary);
    border-radius: var(--radius-md);
  }
  .bt-error {
    color: var(--accent-danger);
  }

  .bt-kpis {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    margin: var(--spacing-sm) 0;
  }
  .bt-kpi {
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: 7px 9px;
    display: flex;
    flex-direction: column;
    gap: 2px;
  }
  .bt-kpi-k {
    font-size: 10px;
    color: var(--text-tertiary);
  }
  .bt-kpi-v {
    font-size: 15px;
    font-weight: 600;
    color: var(--text-primary);
  }
  .bt-kpi-x {
    font-size: 10px;
  }
  /* A股口径：红＝涨/盈利，绿＝跌/亏损（与 main.css 里 --accent-danger 注释"红：A股涨"一致）。
     类名特意不叫 up/dn —— 叫 up 而配绿色，是这类页最容易写反还看不出的一处。 */
  .gain {
    color: var(--accent-danger);
  }
  .loss {
    color: var(--accent-success);
  }

  .bt-card {
    background: var(--bg-secondary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-md);
    padding: var(--spacing-sm);
    margin-bottom: var(--spacing-sm);
  }
  .bt-card-h {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
  }
  .bt-card-h h3 {
    margin: 0 0 6px;
    font-size: 13px;
    color: var(--text-primary);
  }
  .bt-note {
    font-size: 10px;
    color: var(--text-tertiary);
  }
  .bt-legend {
    display: flex;
    flex-wrap: wrap;
    gap: 4px;
    margin-bottom: 6px;
  }
  .bt-legend-2 {
    margin-top: 4px;
  }
  .bt-note {
    font-size: 11px;
    color: var(--text-tertiary);
    background: var(--bg-tertiary);
    border: 1px dashed var(--border-color);
    border-radius: var(--radius-sm);
    padding: 5px 8px;
    margin-bottom: 6px;
  }
  .bt-leg {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-size: 10px;
    padding: 3px 7px;
    border-radius: var(--radius-pill);
    border: 1px solid var(--border-subtle);
    background: var(--bg-primary);
    color: var(--text-tertiary);
    cursor: pointer;
  }
  .bt-leg.on {
    color: var(--text-primary);
    border-color: var(--border-medium);
    background: var(--bg-elevated);
  }
  /* 线样样本：颜色＝卖法、线型＝入口，图例与表格共用同一套画法 */
  .bt-swatch {
    display: inline-block;
    width: 14px;
    height: 0;
    border-top-width: 2px;
    border-top-style: solid;
    vertical-align: middle;
    margin-right: 4px;
  }

  .bt-chart {
    width: 100%;
    height: 230px;
    display: block;
    touch-action: manipulation;
  }
  /* 胜率走势那张矮一截：一页要放两张图，超过 1.5 屏就得一直滚 */
  .bt-chart-2 {
    height: 160px;
  }
  .bt-grid {
    stroke: var(--border-subtle);
    stroke-width: 1;
  }
  .bt-zero {
    stroke: var(--border-medium);
    stroke-width: 1;
    stroke-dasharray: 4 3;
  }
  .bt-cursor {
    stroke: var(--text-tertiary);
    stroke-width: 1;
    stroke-dasharray: 3 3;
  }
  .bt-line {
    fill: none;
    stroke-linejoin: round;
  }
  .bt-ax {
    font-size: 9px;
    fill: var(--text-tertiary);
  }
  .bt-ax-x {
    text-anchor: middle;
  }
  .bt-ax-r {
    text-anchor: end;
  }
  .bt-read {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    align-items: center;
    font-size: 11px;
    color: var(--text-secondary);
    padding-top: 6px;
    border-top: 1px solid var(--border-subtle);
  }
  .bt-read-d {
    font-weight: 600;
    color: var(--text-primary);
  }
  .bt-read-i {
    display: inline-flex;
    align-items: center;
    gap: 4px;
  }
  .bt-read-hint {
    color: var(--text-tertiary);
  }
  .bt-mini {
    font-size: 10px;
    padding: 2px 8px;
    border-radius: var(--radius-pill);
    cursor: pointer;
    border: 1px solid var(--border-subtle);
    background: var(--bg-primary);
    color: var(--text-secondary);
  }

  .bt-det summary {
    cursor: pointer;
    font-size: 12px;
    color: var(--text-secondary);
  }
  .bt-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
    margin-top: 8px;
  }
  .bt-table th,
  .bt-table td {
    text-align: right;
    padding: 4px 5px;
    border-bottom: 1px solid var(--border-subtle);
    color: var(--text-secondary);
  }
  .bt-table th {
    color: var(--text-tertiary);
    font-weight: 500;
    font-size: 10px;
  }
  .bt-table .bt-name {
    text-align: left;
  }
  .bt-table tr.dim {
    opacity: 0.45;
  }
  .bt-why {
    margin-top: 10px;
    font-size: 11px;
    color: var(--text-secondary);
    line-height: 1.7;
  }
  .bt-why ul {
    margin: 4px 0 4px var(--spacing-md);
    padding: 0;
  }
  .bt-why li {
    margin-bottom: 4px;
  }
  .bt-fine {
    font-size: 10px;
    color: var(--text-tertiary);
    margin-top: 6px;
  }
  /* 每日监控点：入口口径差是这条路线图的验收指标，所以它得常驻，不能藏进折叠区 */
  .bt-daily {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--border-subtle);
  }
  .bt-modes {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid var(--border-subtle);
  }
  .bt-daily-h {
    display: flex;
    justify-content: space-between;
    gap: 8px;
    font-size: 11px;
    color: var(--text-secondary);
  }
  .bt-daily-h em {
    font-style: normal;
    color: var(--text-tertiary);
    font-size: 10px;
  }
  .bt-daily-t {
    width: 100%;
    border-collapse: collapse;
    font-size: 11px;
    margin-top: 4px;
  }
  .bt-daily-t th,
  .bt-daily-t td {
    text-align: right;
    padding: 2px 5px;
    color: var(--text-secondary);
  }
  .bt-daily-t th {
    color: var(--text-tertiary);
    font-weight: 500;
    font-size: 10px;
  }
  .bt-daily-t td:first-child,
  .bt-daily-t th:first-child {
    text-align: left;
  }
  .bt-why code {
    font-size: 10px;
    background: var(--bg-primary);
    padding: 1px 4px;
    border-radius: 3px;
  }

  @media (max-width: 760px) {
    .bt-kpis {
      grid-template-columns: repeat(2, 1fr);
    }
    .bt-chart {
      height: 190px;
    }
    .bt-chart-2 {
      height: 150px;
    }
  }
  @media (max-width: 480px) {
    .bt-table {
      font-size: 10px;
    }
    .bt-leg {
      font-size: 9px;
      padding: 2px 6px;
    }
  }
</style>
