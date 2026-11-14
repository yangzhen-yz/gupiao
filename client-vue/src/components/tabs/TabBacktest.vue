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
          <span class="bt-kpi-k">能操作的那条（尾盘买·封板剔除）· 次开卖</span>
          <span class="bt-kpi-v" :class="signCls(head.retPct)">{{ fmt(head.end) }}</span>
          <span class="bt-kpi-x" :class="signCls(head.retPct)"
            >{{ pct(head.retPct) }} ｜ vs 背景 {{ vsBenchTxt(head) }}</span
          >
        </div>
        <div class="bt-kpi">
          <span class="bt-kpi-k">按推荐执行（封板票也当买到了）· 次开卖</span>
          <span class="bt-kpi-v" :class="signCls(head2.retPct)">{{ fmt(head2.end) }}</span>
          <span class="bt-kpi-x" :class="signCls(head2.retPct)"
            >{{ pct(head2.retPct) }} ｜ vs 背景 {{ vsBenchTxt(head2) }}</span
          >
        </div>
        <div class="bt-kpi">
          <span class="bt-kpi-k">最深回撤（本金 {{ fmt(capital) }} 起）</span>
          <span class="bt-kpi-v loss">−{{ (head.mdd || 0).toFixed(1) }}%</span>
          <span class="bt-kpi-x">从你自己的前高往下量，见下方回撤图</span>
        </div>
        <div class="bt-kpi">
          <span class="bt-kpi-k">同期等权背景（{{ bench ? bench.count : '—' }} 只）</span>
          <span v-if="bench" class="bt-kpi-v" :class="signCls(bench.avgPct)">{{ pct(bench.avgPct) }}</span>
          <button v-else class="bt-mini" :disabled="benchLoading" @click="loadBench">
            {{ benchLoading ? '算背景…' : '算一下' }}
          </button>
          <span v-if="bench" class="bt-kpi-x">事后口径，只用来判行情冷热，不算严格基准</span>
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
            线型＝入口（实线＝按推荐执行，虚线＝尾盘买不到就剔除，点线＝排板全成交的上界，现实做不到）。
            首屏默认画三条：<b>虚线（尾盘买·封板剔除）是你真能操作的那条</b>，实线把"封板买不到"也当成买到了，
            点线只是上界——三条都在线上，别看错成"策略赚不到钱"，也别把点线当预期。</span
          >
        </div>

        <header class="bt-card-h bt-dd-h">
          <h3>回撤（水下曲线）</h3>
          <em class="bt-dd-tip">0 ＝ 在你自己之前的最高点；越往下＝离前高越远。创新高就回到 0，横轴与上图同一天轴。</em>
        </header>
        <svg
          class="bt-chart bt-chart-3"
          :viewBox="`0 0 ${W} ${H3}`"
          preserveAspectRatio="none"
          @click="onTap"
          @touchstart.passive="onTap"
        >
          <line
            v-for="g in yGrids3"
            :key="'dg' + g.v"
            :x1="PAD_L"
            :x2="W - PAD_R"
            :y1="g.y"
            :y2="g.y"
            class="bt-grid"
          />
          <text v-for="g in yGrids3" :key="'dt' + g.v" :x="PAD_L - 6" :y="g.y + 3" class="bt-ax">{{ g.label }}</text>
          <text v-for="t in xTicks" :key="'dx' + t.i" :x="t.x" :y="H3 - 6" class="bt-ax bt-ax-x">{{ t.label }}</text>
          <line :x1="PAD_L" :x2="W - PAD_R" :y1="yOf3(0)" :y2="yOf3(0)" class="bt-zero" />
          <polyline
            v-for="c in visible"
            :key="'dl' + c.key"
            :points="path3(c)"
            class="bt-line"
            :stroke="hueOf(c)"
            :stroke-width="2"
            :stroke-dasharray="dashOf(c)"
          />
          <g v-if="readIdx !== null">
            <line :x1="xOf(readIdx)" :x2="xOf(readIdx)" :y1="PAD_T" :y2="H3 - PAD_B" class="bt-cursor" />
            <circle
              v-for="c in visible"
              :key="'dp' + c.key"
              :cx="xOf(readIdx)"
              :cy="yOf3((ddMap[c.key] || [])[readIdx] === undefined ? 0 : ddMap[c.key][readIdx])"
              r="3"
              :fill="hueOf(c)"
            />
          </g>
        </svg>
        <div class="bt-read">
          <template v-if="readRow">
            <span class="bt-read-d">{{ readRow.date }} 距前高</span>
            <span v-for="c in visible" :key="'dr' + c.key" class="bt-read-i">
              <i class="bt-swatch" :style="{ borderColor: hueOf(c), borderStyle: lineStyle(c) }"></i>{{ short(c) }}
              <b class="loss">{{ ((ddMap[c.key] || [])[readIdx] ?? 0).toFixed(1) }}%</b>
            </span>
          </template>
          <span v-else class="bt-read-hint">
            每条线最深的一格：
            <span v-for="c in visible" :key="'dw' + c.key">
              <i class="bt-swatch" :style="{ borderColor: hueOf(c), borderStyle: lineStyle(c) }"></i>{{ short(c) }}
              <b class="loss">−{{ Math.abs(ddWorst(c)).toFixed(1) }}%</b>
              {{ c.key === 'tailclose_open' ? '（≈ 表里 mdd，口径同一份 nav）' : '' }}
            </span>
            —— 回撤深度比"期末是正还是负"更能说明这套买法拿不拿得住。
          </span>
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
          <div class="bt-daily-h">
            <span>模式胜率榜</span>
            <em
              >标签从 10-19 的批次开始才有，此前记录都归"其他"<template v-if="reviewGenAt">
                · 汇总生成于 {{ reviewGenAt }}</template
              ></em
            >
          </div>
          <table class="bt-daily-t">
            <thead>
              <tr>
                <th>模式</th>
                <th>条数</th>
                <th>天数</th>
                <th>名次</th>
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
                <td>{{ m.days != null ? m.days + ' 天' : '—' }}</td>
                <td :class="signCls(m.rankDelta || 0)" :title="rankTip">{{ rankText(m) }}</td>
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
            这里比的是<b>被它挡掉的票</b>和<b>它放行的票</b>第二天谁涨得好。 挡掉的确实更差 → 值得搬到线上；反过来更好 →
            这套规则在削收益，不能上。 判定要攒满 {{ sh.minDays || 10 }} 个<b>有真放行名单</b>的交易日（名单是 10-18
            才开始整份存的）， 不够就只报数不给结论。
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
            口径：当日复权收盘 → 次日复权收盘（两组同成本基准才可比）。 实现在
            <code>server/scripts/shadow_eval.py</code>，命令行跑
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
                <th>vs 背景</th>
                <th>最深回撤</th>
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
                <td :class="signCls(vsBench(c))">{{ vsBenchTxt(c) }}</td>
                <td class="loss">−{{ c.mdd.toFixed(1) }}%</td>
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

        <details class="bt-det" :open="!!excess">
          <summary>
            次日开盘买入、拿 1/3/5/10 天：扣费后到底跑赢没有
            <em v-if="excess && excess.byDay['5']" :class="signCls(excess.byDay['5'].excess)">
              5 日超额 {{ pct(excess.byDay['5'].excess) }}
            </em>
            <em v-else-if="excessError" class="bt-ex-err">{{ excessError }}</em>
            <em v-else>正在算（冷启动约 8 秒）…</em>
          </summary>
          <template v-if="excess && excess.byDay['1']">
            <div class="bt-fine">
              {{ excess.feeModel || '双边费用按模拟器费率' }}；滑点情景：
              <select v-model.number="slippageBps" @change="loadExcess">
                <option :value="0">0 bp/边（理想成交）</option>
                <option :value="10">10 bp/边（默认假设）</option>
                <option :value="20">20 bp/边（较保守假设）</option>
              </select>
              当前结果：{{
                excess.slippageBpsPerSide == null
                  ? '服务端未返回滑点口径'
                  : `${excess.slippageBpsPerSide} bp/边（假设值，非实测）`
              }}；买价上浮、卖价下调。
            </div>
            <table class="bt-table bt-ex-table">
              <thead>
                <tr>
                  <th>持有</th>
                  <th>配对 / 可算样本</th>
                  <th>扣费后均值</th>
                  <th>同期等权基准</th>
                  <th>超额</th>
                  <th>胜率</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="n in ['1', '3', '5', '10']" :key="'ex' + n">
                  <template v-if="excess.byDay[n]">
                    <td>{{ n }} 日</td>
                    <td>{{ excess.byDay[n].pairedN ?? '—' }} / {{ excess.byDay[n].totalN ?? '—' }}</td>
                    <td :class="signCls(excess.byDay[n].avg)">{{ pct(excess.byDay[n].avg) }}</td>
                    <td :class="signCls(excess.byDay[n].benchAvg)">{{ pct(excess.byDay[n].benchAvg) }}</td>
                    <td :class="signCls(excess.byDay[n].excess)">{{ pct(excess.byDay[n].excess) }}</td>
                    <td>{{ excess.byDay[n].winRate == null ? '—' : excess.byDay[n].winRate + '%' }}</td>
                  </template>
                </tr>
              </tbody>
            </table>
            <div class="bt-why">
              <p>
                这条回答的是"<b>这些票拿几天到底行不行</b>"，买价统一用<b>推荐日下一交易日开盘</b>， 不是推荐时的参考价
                —— 后者混着推荐当天已经涨过的那段，算出来会好看得不真实。
                基准是选股范围内<b>全部票</b>在同一买入日、同一持有期等权，逐条配对后平均，
                所以超额不是"策略均值减指数涨幅"这种假 alpha。推荐与基准应用同一套手续费及双边滑点情景。
              </p>
              <p class="bt-fine">
                窗口 {{ excess.since }} ~ {{ excess.until }} / {{ excess.recCount }} 条推荐 （固定 6
                个月，与上方曲线窗口无关）；当日封死涨停的票按"次日竞价能买"处理， 次日一字涨停买不到跳过
                {{ excess.skipped['次日一字涨停买不到'] || 0 }} 条， 其余 K 线不足的样本单独计数、不进分母。
              </p>
              <p v-if="monthly5" class="bt-fine">逐月 5 日超额：{{ monthly5 }}</p>
              <p class="bt-fine">
                负超额的意思很直接：这批推荐次日开盘追进去、拿住，扣完费还不如同日买范围内一篮子。
                它和上面六条曲线不矛盾 —— 上面回答"次日就卖"，这里回答"拿几天"。
              </p>
              <p class="bt-fine">
                历史基准股票范围按当前选股范围回看，存在幸存者偏差；滑点为情景假设，不是逐笔实测。
                前向样本仍需单独验证后才能判断策略是否有稳定超额。
              </p>
            </div>
          </template>
        </details>

        <details class="bt-det" :open="false">
          <summary>
            因子体检（每周六 08:30，本地库离线跑）
            <em v-if="labView" :class="signCls(labView.recExcess)"> 5 日超额 {{ pct(labView.recExcess) }} </em>
            <em v-else-if="labError" class="bt-ex-err">{{ labError }}</em>
            <em v-else>暂无体检结果</em>
          </summary>
          <template v-if="labView">
            <table class="bt-table bt-ex-table">
              <thead>
                <tr>
                  <th>因子</th>
                  <th>5 日 IC</th>
                  <th>t 值</th>
                  <th>Q5-Q1</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="f in labRows" :key="'lab' + f.k">
                  <td>{{ f.label }}</td>
                  <td :class="signCls(f.ic)">{{ f.ic > 0 ? '+' : '' }}{{ f.ic.toFixed(3) }}</td>
                  <td>{{ f.t > 0 ? '+' : '' }}{{ f.t.toFixed(2) }}</td>
                  <td :class="signCls(f.q)">{{ f.q > 0 ? '+' : '' }}{{ f.q.toFixed(2) }}%</td>
                </tr>
              </tbody>
            </table>
            <div class="bt-why">
              <p>
                横截面 Spearman IC，|t|&gt;2 才算有点信号。这份样本里
                <b>20 日反转最稳，缩量与最大单日涨幅取负也过 |t|&gt;3；</b>
                <b>负 IC 端最强的是涨停数与流动性</b>
                —— RPS 本质就是 60 日动量，所以只落影子、不直接加权。因子好不代表改评分就能赚钱， 还要过"同范围超额 +
                回撤不恶化"那道门槛。
              </p>
              <p class="bt-fine">
                窗口 {{ (labView.window || [])[0] }} ~ {{ (labView.window || [])[1] }} / {{ labView.rebalances }} 个截面
                / {{ labView.universe }} 只范围；推荐 {{ labView.recN }} 条，5 日扣费均值 {{ pct(labView.recMean) }}、
                超额 {{ pct(labView.recExcess) }}。只读本地日K、不联网，结果落 <code>eval_lab_runs</code>。
              </p>
            </div>
          </template>
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
  const reviewGenAt = ref('') // /api/recommend-review 的 generated_at：模式榜数据是什么时候算的
  const sh = ref({ rows: [], note: '', minDays: 10 })
  const readIdx = ref(null)
  const excess = ref(null) // 次日开盘持有 N 日的可执行超额（独立接口，后到）
  const excessError = ref('')
  const slippageBps = ref(10)
  const lab = ref(null) // 最近一次因子体检（每周六离线跑）
  const labError = ref('')
  const benchLoading = ref(false) // 同期等权背景单独要 2~3 秒，先出曲线再后台补它

  const W = 760,
    H = 230,
    H2 = 160,
    H3 = 140, // 回撤（水下）图：只看 0 到最深，比资金曲线矮
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
  const byKey = k => curves.value.find(c => c.key === k) || { retPct: 0, end: capital, nav: [], mdd: 0 }
  // 首屏默认画哪几条（2026-11-02 改）：接口给的 `default` 标的是两条"按推荐执行"（auction_*），
  // 而那两条把"封板买不到"也算进仓位，一进来就是两条 −25% 以下的下线，读不出"真能操作的那条"
  // 其实只亏 1~2 个点。所以首屏固定三条：能操作的（尾盘买、封板剔除）→ 按推荐执行（含买不到）→
  // 排板全成交（上界，现实做不到）。图例仍可自由开关，这里只决定第一次看到什么。
  const PRIOR_SHOWN = ['tailclose_open', 'auction_open', 'filled_open']
  const head = computed(() => byKey('tailclose_open')) // 主卡＝真能操作的那条
  const head2 = computed(() => byKey('auction_open')) // 次卡＝按推荐执行（含买不到的）
  const bench = computed(() =>
    data.value && data.value.benchmark && data.value.benchmark.available ? data.value.benchmark : null
  )
  // 相对同期等权背景（bench 到位才算，算不出就显示 —，不假装跑赢）
  const vsBench = c => (bench.value ? c.retPct - bench.value.avgPct : null)
  const vsBenchTxt = c => {
    const v = vsBench(c)
    return v == null ? '—' : `${v > 0 ? '+' : ''}${v.toFixed(1)}pp`
  }
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
  // 名次列（2026-10-30 借鉴老王量化组合榜）：#N + 较前一交易日的升降（▲红=上升，▼绿=下降）
  const rankTip = '按 T+1 胜率排名（样本 ≥5 才参与）；▲▼ 是与截至前一交易日名次比的升降'
  const rankText = m => {
    if (m.rank == null) return '—'
    const base = `#${m.rank}`
    if (m.rankDelta == null) return base
    if (m.rankDelta > 0) return `${base} ▲${m.rankDelta}`
    if (m.rankDelta < 0) return `${base} ▼${-m.rankDelta}`
    return `${base} —`
  }
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
  const short = c => (c.sideLabel ? `${c.sideLabel}·${c.modeLabel}` : `${c.entryLabel.split('（')[0]}·${c.modeLabel}`)

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
    // 末点必须标出来（那是"到今天"），但离上一格太近会叠成 "10-30-02" 这种糊字：
    // 太近就替换掉上一格，而不是再挤一个进去（2026-11-02 实拍发现）
    if (out[out.length - 1].i !== n - 1) {
      const last = { i: n - 1, x: xOf(n - 1), label: allDates.value[n - 1].slice(5) }
      if (n - 1 - out[out.length - 1].i < Math.max(2, step / 2)) out[out.length - 1] = last
      else out.push(last)
    }
    return out
  })
  const readRow = computed(() => (readIdx.value === null ? null : { date: allDates.value[readIdx.value] }))

  /* ---------- 回撤（水下曲线，2026-11-02 加）----------
   * 为什么单开一张：资金曲线全在本金线下时，人只看得出"在亏"，看不出"什么时候最深、
   * 有没有回到过前高"。这里画的是**相对自己之前最高点的跌幅**：0 线＝当时的峰值，
   * 越往下越深；一旦创新高就回到 0。表里那个 mdd 是这张图的最低点，两者必须对得上。
   * 口径与上面完全同源（同一批 nav 点），不另算一遍。 */
  const BOX3 = { w: W, h: H3, pl: PAD_L, pr: PAD_R, pt: PAD_T, pb: PAD_B }
  const ddSeriesOf = c => {
    let peak = capital
    return seriesOf(c).map(v => {
      if (v > peak) peak = v
      return (v / peak - 1) * 100
    })
  }
  const ddMap = computed(() => {
    const m = {}
    visible.value.forEach(c => {
      m[c.key] = ddSeriesOf(c)
    })
    return m
  })
  const ddLo = computed(() => {
    let lo = 0
    Object.values(ddMap.value).forEach(s => s.forEach(v => (lo = Math.min(lo, v))))
    // 最深 0（全都一路新高）时也给出一小段下轴，免得图贴着 0 线看不出深浅
    return lo < -1 ? lo * 1.08 : -5
  })
  const yOf3 = v => yAt(v, ddLo.value, 0, BOX3)
  const path3 = c => polyline(ddMap.value[c.key] || [], ddLo.value, 0, BOX3)
  const yGrids3 = computed(() => gridValues(ddLo.value, 0, 3).map(v => ({ v, y: yOf3(v), label: v.toFixed(0) + '%' })))
  // 图上直接标出每条线的最深回撤（与表里 mdd 同一算法），省得看图的人再去对数字
  const ddWorst = c => {
    const s = ddMap.value[c.key] || []
    return s.length ? Math.min(...s) : 0
  }

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

  async function loadSim(withBench, keepShown) {
    loading.value = withBench ? false : true
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
      // 后台补基准时不要把用户已经开关过的图例重置掉
      if (!keepShown || !shown.value.size) {
        const keys = (data.value.curves || []).map(c => c.key)
        const init = PRIOR_SHOWN.filter(k => keys.includes(k))
        shown.value = new Set(init.length ? init : keys.slice(0, 2))
      }
      readIdx.value = null
    } catch (e) {
      error.value = '回测曲线加载失败：' + (e.message || e)
    } finally {
      loading.value = false
    }
  }
  async function loadBench() {
    if (benchLoading.value) return
    benchLoading.value = true
    try {
      // 同一接口带 bench=1：曲线部分 30 分钟缓存里已有，多花的是背景那 2~3 秒
      await loadSim(true, true)
    } finally {
      benchLoading.value = false
    }
  }
  // 可执行超额单独请求：它冷算实测 7.4 秒（6 个月 × 全范围等权基准），不能让主曲线等它。
  // 超时单独放宽到 30s 并自动重试一次（2026-10-27）：默认 10s 在服务冷启动时必然超时——
  // 手机实拍「超额没算出来：请求超时（10s）」，而第二次请求命中服务端缓存只要几毫秒。
  // 注意 `apiFetch` **只在网络错误/超时/401 抛异常，HTTP 500 是 resolve 一个 {detail} 对象的**
  // （09:28 实测：桩一个 500 只发出一次请求、页面也没有红字），所以"算失败"必须自己判结构：
  // 不判就会把 undefined 塞进 excess，标题永远挂着"正在算（冷启动约 8 秒）…"骗人。
  let excessRequestId = 0
  async function loadExcess() {
    const requestId = ++excessRequestId
    excessError.value = ''
    for (let attempt = 1; attempt <= 2; attempt++) {
      let d = null
      let why = ''
      try {
        d = await apiFetch(`/api/recommend-excess?months=6&slippage_bps=${slippageBps.value}`, {
          timeout: 30000,
        })
      } catch (e) {
        why = e.message || String(e)
      }
      if (requestId !== excessRequestId) return
      if (d && d.success && d.data) {
        excess.value = d.data
        excessError.value = ''
        return
      }
      why = why || (d && (d.detail || d.error)) || '服务端返回结构不对'
      if (attempt === 2) excessError.value = '超额没算出来：' + why
    }
  }
  // 因子体检是周更的离线结果，读最近一次即可；读不到就在标题上说明，不静默隐藏
  async function loadLab() {
    labError.value = ''
    try {
      const d = await apiFetch('/api/eval-lab/latest')
      lab.value = (d && d.data) || null
    } catch (e) {
      labError.value = '体检结果读不到：' + (e.message || e)
    }
  }
  const monthly5 = computed(() => {
    const bm = (excess.value && excess.value.byMonth) || {}
    return Object.keys(bm)
      .sort()
      .filter(m => bm[m] && bm[m]['5'])
      .map(m => `${m.slice(5)}月 ${pct(bm[m]['5'].excess)}`)
      .join(' · ')
  })
  const LAB_LABELS = {
    rev20: '20日反转',
    rev5: '5日反转',
    mom60: '60日动量',
    prox60: '距60日高点',
    ma20_gap: '偏离MA20',
    lowvol20: '20日低波',
    vol_shrink: '缩量',
    maxret20: '20日最大单日',
    liq20: '20日流动性',
    limit20: '20日涨停数',
  }
  // 按 IC 从高到低排：正 IC 在上、负 IC 在下，一眼能看出"该加谁、该躲谁"
  const labRows = computed(() => {
    const fs = (lab.value && lab.value.factors) || {}
    return Object.keys(fs)
      .map(k => {
        const f5 = (fs[k] && fs[k].fwd5) || {}
        const q = (fs[k] && fs[k].q5) || {}
        return {
          k,
          label: LAB_LABELS[k] || k,
          ic: Number(f5.meanIC ?? 0),
          t: Number(f5.t ?? 0),
          q: Number(q.q5_q1 ?? 0),
        }
      })
      .sort((a, b) => b.ic - a.ic)
  })
  // 把推荐段拉平，模板里不用层层可选链
  const labView = computed(() => {
    const d = lab.value
    if (!d || !d.available) return null
    const r = d.recommendations || {}
    const ret5 = r.ret5 || {}
    return {
      ...d,
      recN: r.n ?? 0,
      recMean: ret5.mean ?? 0,
      recExcess: ret5.excessMean ?? 0,
    }
  })
  function setMonths(m) {
    months.value = m
    loadSim(!!bench.value)
  }

  onMounted(async () => {
    // 曲线先出（服务端 30 分钟缓存里就那份），同期等权背景要再多 2~3 秒 → 后台补，
    // 不挡首屏；但**必须自动补**，否则默认态只有绝对收益，满屏负数没参照（2026-11-02 改）
    await loadSim(false)
    loadBench()
    loadExcess()
    loadLab()
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
      reviewGenAt.value = (d.data && d.data.generated_at) || ''
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
  /* 回撤图的标题行：一句"0 是什么"必须贴在图上方，不然水下曲线会被读反 */
  .bt-dd-h {
    margin-top: 10px;
  }
  .bt-dd-tip {
    font-size: 10px;
    font-style: normal;
    color: var(--text-tertiary);
    flex: 1 1 240px;
    min-width: 0;
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
  /* 回撤（水下）更矮：它是资金曲线的伴图，不单独占一屏 */
  .bt-chart-3 {
    height: 140px;
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
  .bt-det summary em {
    margin-left: 8px;
    font-style: normal;
    font-weight: 600;
  }
  .bt-det summary em.bt-ex-err {
    color: var(--accent-warning);
    font-weight: 500;
  }
  .bt-ex-table {
    max-width: 560px;
  }
  .bt-ex-table th:first-child,
  .bt-ex-table td:first-child {
    text-align: left;
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
    .bt-chart-3 {
      height: 130px;
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
