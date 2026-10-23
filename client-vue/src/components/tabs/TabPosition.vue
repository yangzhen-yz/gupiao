<template>
  <div>
    <div class="section-title">持仓管理与仓位配置</div>
    <div class="section-subtitle">记录持仓，自动诊断止损/止盈/减仓信号，按大盘环境给出总仓位建议——大赚小亏靠纪律</div>

    <!-- 操作指南 -->
    <div class="guide-box">
      <div class="guide-header" @click="guideOpen = !guideOpen">
        <span>📖 实战操作指南（两个Tab怎么用 · 四时段挂单 · 买卖纪律）</span>
        <span class="guide-toggle">{{ guideOpen ? '收起 ▲' : '展开 ▼' }}</span>
      </div>
      <div v-show="guideOpen" class="guide-body">
        <div class="guide-section">
          <div class="guide-title">① 看哪个Tab？</div>
          <div class="guide-item">
            <b>智能推荐</b>（主决策）：每日精选≤3只，已带买点/卖点/时机徽章/风控止损——<b>买它，并按卡片时机执行</b>
          </div>
          <div class="guide-item">
            <b>股票筛选</b
            >（发现机会）：全市场盘中实时打分排序，用来扩大视野；看到高分票可先观察，等它进推荐榜或自行按同等纪律参与
          </div>
          <div class="guide-item"><b>持仓管理</b>（本页）：管已买入的票，止损止盈减仓一句话说清</div>
        </div>
        <div class="guide-section">
          <div class="guide-title">② 什么时候挂单？（按卡片「买入时机」徽章）</div>
          <table class="guide-table">
            <tr>
              <th>徽章</th>
              <th>时段</th>
              <th>适合的票</th>
              <th>挂法</th>
            </tr>
            <tr>
              <td>🔔 次日竞价</td>
              <td>9:15–9:25</td>
              <td>昨日涨停封板、强势龙头</td>
              <td>9:20后挂竞价单；<b>高开5%以上不追</b>，没成交就撤</td>
            </tr>
            <tr>
              <td>⚡ 早盘买入</td>
              <td>9:30–10:00</td>
              <td>强势情绪股（龙头/先锋）</td>
              <td>开盘回踩不破分时均价线再买，<b>不追直线拉升</b>；10点后不再新开早盘仓</td>
            </tr>
            <tr>
              <td>📉 回踩低吸</td>
              <td>10:00–14:00</td>
              <td>低位趋势股（中军/补涨）</td>
              <td>按「买入参考」价挂低吸单等回踩，<b>跌破均价线或涨幅转负就撤单</b></td>
            </tr>
            <tr>
              <td>🌙 尾盘买入</td>
              <td>14:30–15:00</td>
              <td>高振幅/冲高回落/高位票</td>
              <td>14:30后确认全天量价形态再买，<b>避开尾盘急拉追高</b></td>
            </tr>
          </table>
        </div>
        <div class="guide-section">
          <div class="guide-title">③ 买卖纪律（大赚小亏的核心）</div>
          <div class="guide-item">
            💰 <b>买</b>：只按卡片「买入参考」价低吸或对应时段买；复盘132次证明现价追入胜率仅45%，低吸才有63%
          </div>
          <div class="guide-item">
            🎯
            <b>盈</b>：到「目标卖点」且当日走弱→止盈；浮盈≥5%止损上移成本价，浮盈≥10%上移成本+5%（移动止盈，本页自动算）
          </div>
          <div class="guide-item">
            🛑 <b>亏</b>：现价跌破「止损价」（-6%~-12%，按振幅缩放）<b>无条件执行</b>，不抢反弹、不摊平
          </div>
          <div class="guide-item">
            ⚖️ <b>仓位</b>：单票≤20%（高分龙头≤30%），总仓位跟随本页「建议总仓位」随大盘调整；持仓超5只或单票超30%会告警
          </div>
        </div>
      </div>
    </div>

    <!-- 交易环境提示（今日适不适合交易，60s刷新及时可见） -->
    <div v-if="data.envAdvice" class="env-banner" :class="'env-' + data.envAdvice.level">
      <div class="env-main">
        <span class="env-emoji">{{ data.envAdvice.emoji }}</span>
        <div class="env-text">
          <div class="env-title">
            今日交易环境：{{ data.envAdvice.title }}
            <small
              >大盘 {{ data.envAdvice.marketChange >= 0 ? '+' : '' }}{{ data.envAdvice.marketChange }}%<template
                v-if="data.envAdvice.openChange != null"
              >
                · 开盘 {{ data.envAdvice.openChange >= 0 ? '+' : '' }}{{ data.envAdvice.openChange }}%</template
              ><template v-if="data.envAdvice.sentiment">
                · 情绪{{ data.envAdvice.sentiment.cycle }}期({{ data.envAdvice.sentiment.cycleScore }}分)</template
              ></small
            >
          </div>
          <div class="env-detail">{{ data.envAdvice.detail }}</div>
          <div v-if="data.envAdvice.notes?.length" class="env-notes">
            修正因素：{{ data.envAdvice.notes.join('；') }}
          </div>
        </div>
        <div class="env-pct">
          建议总仓位<br /><b>{{ data.envAdvice.pct }}%</b>
        </div>
      </div>
    </div>

    <!-- 组合总览 -->
    <div v-if="data.portfolio" class="portfolio-box">
      <!-- 初始本金：仓位口径的分母。不设就只能拿持仓市值除自己，只持一只票必然显示 100% -->
      <div class="capital-row">
        <span class="capital-label">初始本金</span>
        <template v-if="!editingCapital">
          <b class="capital-value" :class="{ unset: !data.portfolio.capital_set }">
            {{ data.portfolio.capital_set ? fmtMoney(data.portfolio.principal) : '未设置' }}
          </b>
          <span class="capital-pct">
            实际仓位
            <b v-if="data.portfolio.capital_set">{{ data.portfolio.position_pct }}%</b>
            <b v-else>--</b>
            <small>{{
              data.portfolio.capital_set ? data.portfolio.base_label + '口径' : '设了本金才按真实资金占比算'
            }}</small>
          </span>
          <span v-if="data.portfolio.capital_set" class="capital-free"
            >可用 {{ fmtMoney(data.portfolio.free_cash) }}</span
          >
          <button class="capital-edit" @click="startEditCapital">
            {{ data.portfolio.capital_set ? '修改' : '设置' }}
          </button>
          <!-- 当前资金 / 总盈亏（2026-10-23）：初始本金是死数，清仓后赚的钱只体现在这里。
               总盈亏 = 已实现（减仓/清仓落账）+ 浮动（持仓市值-成本），不依赖本金设置；
               当前资金 = 初始本金 + 总盈亏，要设了本金才有分母。 -->
          <span
            v-if="data.portfolio.capital_set || (data.portfolio.total_pnl_all || 0) !== 0"
            class="capital-sub-row"
            :title="`已实现 ${fmtMoney(data.portfolio.realized_pnl || 0)} · 浮动 ${fmtMoney(
              (data.portfolio.total_pnl_all || 0) - (data.portfolio.realized_pnl || 0)
            )}`"
          >
            <span v-if="data.portfolio.capital_set" class="capital-sub-item">
              当前资金 <b>{{ fmtMoney(data.portfolio.current_capital) }}</b>
            </span>
            <span
              class="capital-sub-item"
              :style="{
                color: (data.portfolio.total_pnl_all || 0) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)',
              }"
            >
              总盈亏<small>（含已实现）</small>
              <b
                >{{ (data.portfolio.total_pnl_all || 0) >= 0 ? '+' : ''
                }}{{ fmtMoney(data.portfolio.total_pnl_all || 0) }}</b
              ><small v-if="data.portfolio.total_pnl_all_pct != null">
                （{{ (data.portfolio.total_pnl_all_pct || 0) >= 0 ? '+' : ''
                }}{{ data.portfolio.total_pnl_all_pct }}%）</small
              >
            </span>
          </span>
        </template>
        <div v-else class="capital-edit-wrap">
          <input
            ref="capitalInputRef"
            v-model="capitalInput"
            type="number"
            min="0"
            step="1000"
            class="form-input capital-input"
            placeholder="如 200000"
          />
          <button class="btn-scan" :disabled="saving" @click="saveCapital">{{ saving ? '保存中...' : '保存' }}</button>
          <button class="btn-secondary" @click="cancelCapital">取消</button>
          <span class="capital-hint">只影响仓位百分比的计算基数，不改动任何买卖信号</span>
        </div>
      </div>
      <!-- 企业微信推送：谁的持仓提醒发给谁。绑定即验证 —— 点「绑定」会真的收到一条确认消息，
           收不到就不算绑上，所以这里不会出现"看着配好了其实从没发出去"的通道。 -->
      <div class="push-row">
        <span class="push-label">📲 企业微信推送</span>
        <template v-if="!editingPush">
          <b class="push-value" :class="{ unset: !(push.channels && push.channels.length) }">
            {{
              push.channels && push.channels.length
                ? `已绑定 ${push.channels.length} 条（${push.channels[0].label || '我的群'}）`
                : '未绑定'
            }}
          </b>
          <span class="push-meta">
            今日 {{ push.sent_today || 0 }}<small v-if="push.daily_limit">/{{ push.daily_limit }}</small> 条
            <small v-if="push.using_global_fallback">· 暂走系统默认群</small>
          </span>
          <button class="capital-edit" @click="startEditPush">
            {{ push.channels && push.channels.length ? '管理' : '绑定' }}
          </button>
        </template>
        <div v-else class="push-edit-wrap">
          <input
            v-model="pushInput"
            type="text"
            class="form-input push-input"
            placeholder="粘贴群机器人 Webhook 地址（https://qyapi.weixin.qq.com/…）"
          />
          <input
            v-model="pushLabel"
            type="text"
            class="form-input push-name-input"
            placeholder="备注名，如「我的止损群」"
          />
          <div class="push-btns">
            <button class="btn-scan" :disabled="savingPush" @click="bindPush">
              {{ savingPush ? '验证中...' : '绑定并发确认消息' }}
            </button>
            <button class="btn-secondary" @click="editingPush = false">收起</button>
          </div>
          <ol class="push-steps">
            <li>企业微信里建一个群（只有你自己也行）</li>
            <li>群设置 → 群机器人 → 添加「自定义机器人」→ 复制 Webhook 地址</li>
            <li>整段粘到上面点绑定，手机上收到「推送绑定成功」即生效</li>
          </ol>
          <div v-if="push.channels && push.channels.length" class="push-list">
            <div v-for="c in push.channels" :key="c.id" class="push-item">
              <span class="push-item-name">{{ c.label || '未命名' }}</span>
              <code class="push-item-url">{{ c.target_hint }}</code>
              <button class="push-mini" :disabled="savingPush" @click="testPush(c.id)">测试</button>
              <button class="push-mini danger" :disabled="savingPush" @click="unbindPush(c.id)">解绑</button>
            </div>
          </div>
          <!-- 企微单聊：走「智能机器人」，授权由腾讯托管页面完成，不需要公网域名、
               不需要管理员建自建应用。绑定方式按省事排序：
               发一句话 → 认领会话 → 姓名搜索 → 手填。 -->
          <div class="push-agent">
            <span class="push-agent-title">企微单聊（智能机器人）</span>
            <span :class="['push-agent-state', bot.ok ? 'ok' : '']">
              {{
                bot.checked
                  ? bot.ok
                    ? '机器人可用，绑定后提醒只发给你本人'
                    : '机器人暂不可用：' + bot.error
                  : '检测中…'
              }}
            </span>
            <div class="push-btns">
              <button class="push-mini" :disabled="bot.busy" @click="refreshSessions">
                {{ bot.busy ? '查询中…' : '① 我给机器人发了消息，刷新会话' }}
              </button>
              <input
                v-model="bot.keyword"
                type="text"
                class="form-input push-name-input"
                placeholder="② 或按姓名查同事"
              />
              <button class="push-mini" :disabled="bot.busy" @click="searchBotUser">搜索</button>
            </div>
            <div v-if="bot.sessions.length || bot.users.length" class="push-list">
              <div v-for="s in bot.sessions" :key="'s' + s.chat_id" class="push-item">
                <span class="push-item-name">{{ s.name || '未命名会话' }}</span>
                <code class="push-item-url">{{ s.chat_id }}</code>
                <button class="push-mini" :disabled="bot.busy" @click="bindAibot(s.chat_id, s.name)">绑定</button>
              </div>
              <div v-for="u in bot.users" :key="'u' + u.userid" class="push-item">
                <span class="push-item-name"
                  >{{ u.name }}<small v-if="u.dept"> · {{ u.dept }}</small></span
                >
                <code class="push-item-url">{{ u.userid }}</code>
                <button class="push-mini" :disabled="bot.busy" @click="bindAibot(u.userid, u.name)">绑定</button>
              </div>
            </div>
            <div class="push-btns">
              <input
                v-model="agentInput"
                type="text"
                class="form-input push-name-input"
                placeholder="③ 已知会话ID/userid，直接填"
              />
              <button class="btn-scan" :disabled="savingPush" @click="bindAibotManual">绑定单聊</button>
            </div>
            <small v-if="!bot.ok" class="push-agent-hint">
              机器人权限没开时，上面会直接显示企业微信给的授权链接文案，点开授权后回来刷新即可；
              单聊绑不上不影响下面的群机器人提醒。
            </small>
          </div>
          <span v-if="pushMsg" :class="['push-msg', pushMsgOk ? 'ok' : 'bad']">{{ pushMsg }}</span>
        </div>
      </div>
      <div class="portfolio-row">
        <div class="portfolio-metric">
          <em>大盘涨跌</em>
          <b :style="{ color: data.market_change >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
            >{{ data.market_change >= 0 ? '+' : '' }}{{ data.market_change }}%</b
          >
        </div>
        <div class="portfolio-metric">
          <em>持仓总盈亏</em>
          <b
            :style="{ color: (data.portfolio?.total_pnl || 0) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
          >
            {{ (data.portfolio?.total_pnl || 0) >= 0 ? '+' : '' }}{{ data.portfolio?.total_pnl }} 元（{{
              (data.portfolio?.total_pnl_pct || 0) >= 0 ? '+' : ''
            }}{{ data.portfolio?.total_pnl_pct }}%）
          </b>
        </div>
      </div>
      <div v-if="data.portfolio?.warnings?.length" class="portfolio-warnings">
        <span v-for="w in data.portfolio.warnings" :key="w" class="warn-chip">⚠ {{ w }}</span>
      </div>
      <div class="portfolio-updated">诊断时间 {{ data.generated_at }} · 交易时段每60秒自动刷新</div>
    </div>

    <!-- 添加/编辑持仓 -->
    <div class="pos-form-box">
      <div class="form-title">{{ editingSymbol ? '✏️ 编辑持仓' : '➕ 添加持仓' }}</div>
      <div class="form-grid">
        <label class="form-field wide">
          <span class="form-label">股票（输代码或名称，支持模糊搜索）</span>
          <div class="combo-wrap">
            <input
              v-model="stockQuery"
              class="form-input"
              placeholder="如：美诺 / 603538 / sh600000"
              @input="onStockQueryInput"
              @focus="onStockQueryInput"
            />
            <div v-if="searchResults.length > 0" class="combo-dropdown">
              <div v-for="s in searchResults" :key="s.symbol" class="combo-item" @mousedown.prevent="selectStock(s)">
                <b class="combo-name">{{ s.name }}</b>
                <span class="combo-code">{{ s.symbol.toUpperCase() }}</span>
                <span v-if="s.price" class="combo-price">现价 {{ s.price }}</span>
              </div>
            </div>
            <div v-else-if="stockQuery && searchLoading" class="combo-dropdown">
              <div class="combo-item combo-searching">搜索中...</div>
            </div>
          </div>
        </label>
        <label class="form-field">
          <span class="form-label">成本价（元）</span>
          <input
            v-model="form.cost_price"
            type="number"
            step="0.01"
            min="0"
            placeholder="如 27.00"
            class="form-input"
          />
        </label>
        <label class="form-field">
          <span class="form-label">持仓数（股，默认100）</span>
          <input v-model="form.shares" type="number" step="100" min="0" placeholder="100" class="form-input" />
        </label>
        <div class="form-field">
          <span class="form-label">现价（自动获取）</span>
          <div class="form-input readonly" :style="{ color: priceColor }">
            {{ currentPrice ? currentPrice : '选择股票后自动显示' }}
          </div>
        </div>
        <div class="form-actions">
          <button class="btn-scan" @click="submitPosition" :disabled="saving">
            {{ saving ? '保存中...' : '保存' }}
          </button>
          <button v-if="editingSymbol" class="btn-secondary" @click="resetForm">取消</button>
        </div>
      </div>
    </div>

    <!-- 持仓列表 -->
    <div v-if="!data.has_positions" class="empty-box">
      📭 还没有持仓记录，先在上方添加一笔（或用智能推荐买入后记在这里）
    </div>
    <div v-else class="pos-table">
      <div class="pos-table-header">
        <span>名称</span><span>成本/现价</span><span>盈亏</span><span>止损价</span><span>目标价</span><span>信号</span
        ><span>建议</span><span style="text-align: center">操作</span>
      </div>
      <template v-for="p in data.positions" :key="p.symbol">
        <div class="pos-table-row">
          <span class="pos-name">
            <b>{{ p.name }}</b>
            <small
              >{{ p.symbol.toUpperCase()
              }}<template v-if="data.portfolio?.capital_set && p.pos_pct != null">
                · 仓位{{ p.pos_pct }}%</template
              ></small
            >
          </span>
          <span class="pos-price">
            {{ p.cost }}<br /><b
              :style="{ color: p.price >= p.cost ? 'var(--accent-danger)' : 'var(--accent-success)' }"
              >{{ p.price }}</b
            >
          </span>
          <span
            class="pos-pnl"
            :style="{ color: (p.pnl_pct || 0) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)' }"
          >
            {{ (p.pnl_pct || 0) >= 0 ? '+' : '' }}{{ p.pnl_pct }}%<br /><small
              >{{ (p.pnl_amt || 0) >= 0 ? '+' : '' }}{{ p.pnl_amt }}元</small
            >
          </span>
          <span class="pos-stop"
            >{{ p.stop_price ?? '--' }}<small v-if="p.trailing?.enabled">移动线{{ p.trailing.line }}</small></span
          >
          <span class="pos-target">{{ p.target_price ?? '--' }}</span>
          <span class="pos-flags"
            ><span v-for="f in p.flags" :key="f" class="flag-chip">{{ f }}</span
            ><span v-if="!p.flags?.length" class="flag-chip ok">无异动</span></span
          >
          <span class="pos-action">
            <span class="action-badge" :class="'action-' + p.action">{{ p.action_label }}</span>
            <small class="pos-detail">{{ p.detail }}</small>
            <!-- 统一卖出计划（2026-10-22）：默认影子展示，不覆盖上方动作；实盘开关打开后才是“实盘” -->
            <small
              v-if="p.exit_plan?.enabled"
              class="exit-plan"
              :class="{ hit: p.exit_plan.hit }"
              :title="p.exit_plan.reason"
            >
              {{ p.exit_plan.live ? '实盘卖出' : '影子卖出' }}：{{ exitPlanText(p.exit_plan) }}
            </small>
          </span>
          <span class="pos-ops">
            <button class="op-btn" @click="startAdjust(p, 'add')">加仓</button>
            <button class="op-btn" @click="startAdjust(p, 'reduce')">减仓</button>
            <button class="op-btn" @click="editPosition(p)">编辑</button>
            <button class="op-btn danger" @click="removePosition(p)">删除</button>
          </span>
        </div>
        <!-- 加减仓：加权成本由后端算，但结果先在这里预览出来 —— 用户点确认前就该看到新成本 -->
        <div v-if="adj.symbol === p.symbol" class="adj-bar">
          <span class="adj-title">{{ adj.mode === 'add' ? '➕ 加仓' : '➖ 减仓' }} {{ p.name }}</span>
          <span class="adj-now">现持 {{ p.shares }} 股 @ {{ p.cost }}</span>
          <input
            v-model="adj.shares"
            type="number"
            min="100"
            step="100"
            class="form-input adj-input"
            :placeholder="adj.mode === 'add' ? '新增股数' : '卖出股数'"
            ref="adjSharesInput"
            @keyup.esc="cancelAdjust"
          />
          <input
            v-model="adj.price"
            type="number"
            step="0.01"
            min="0"
            class="form-input adj-input"
            placeholder="成交价"
          />
          <span class="adj-preview" v-if="adjPreview(p)">{{ adjPreview(p) }}</span>
          <button class="btn-scan" :disabled="adj.saving" @click="submitAdjust(p)">
            {{ adj.saving ? '提交中...' : '确认' }}
          </button>
          <button class="btn-secondary" @click="cancelAdjust">取消</button>
          <span class="adj-note-line" v-if="adj.mode === 'reduce' && Number(adj.shares) >= p.shares">
            卖光 {{ p.shares }} 股会把这条持仓从列表移除
          </span>
        </div>
      </template>
    </div>
  </div>
</template>

<script setup>
  import { ref, computed, inject, onMounted, onUnmounted, nextTick } from 'vue'
  import { apiFetch, isTradingTime, getNextRefreshDelay } from '../../utils/helpers'
  import { usePolling } from '../../composables/usePolling'

  // 轮询治理（2026-10-13）：持仓 60s 价格刷新只在 Tab 可见时跑
  const props = defineProps({ active: { type: Boolean, default: true } })

  const showNotification = inject('showNotification', () => {})

  const data = ref({ has_positions: false, positions: [], portfolio: null, market_change: null })
  const guideOpen = ref(true)
  const saving = ref(false)
  const editingSymbol = ref('')
  const form = ref({ symbol: '', name: '', cost_price: '', shares: '100' })
  // 股票智能搜索（2026-10-12：名称→代码反查 + 模糊匹配）
  const stockQuery = ref('')
  const searchResults = ref([])
  const searchLoading = ref(false)
  const currentPrice = ref(null)
  let searchTimer = null
  // 交易时段 60s 自动刷新（"及时建议"）；2026-10-13 起仅在 Tab 可见时轮询
  const { start: startPositionPolling } = usePolling(() => loadPositions(), {
    interval: () => (isTradingTime() ? 60000 : getNextRefreshDelay()),
    active: () => props.active,
    immediate: false,
  })

  const priceColor = computed(() => {
    const p = parseFloat(currentPrice.value)
    const c = parseFloat(form.value.cost_price)
    if (!p || !c) return 'var(--text-primary)'
    return p >= c ? 'var(--accent-danger)' : 'var(--accent-success)'
  })

  async function loadPositions() {
    try {
      const result = await apiFetch('/api/positions')
      if (result.success) data.value = result.data
    } catch (e) {
      console.error('加载持仓失败:', e)
    }
  }

  // ===== 企业微信推送通道（2026-10-14，按账号自助绑定）=====
  // 绑定即验证：后端先往那个群真发一条确认消息，企微没返回 errcode=0 就不落库。
  // 所以列表里出现的通道都是"确实送达过"的 —— 避免出现"看着配好了、其实几个月没发出去"
  // 这种静默失效（本项目真实踩过一次：wecom-cli 不在 PATH，日志全绿但零送达）。
  const push = ref({ channels: [], sent_today: 0, daily_limit: 0, using_global_fallback: false })
  const editingPush = ref(false)
  const savingPush = ref(false)
  const pushInput = ref('')
  const pushLabel = ref('')
  const pushMsg = ref('')
  const pushMsgOk = ref(true)
  // 智能机器人单聊通道（授权走腾讯托管页面，无需公网域名/自建应用）
  const bot = ref({ checked: false, busy: false, ok: false, error: '', sessions: [], users: [], keyword: '' })
  const agentInput = ref('')

  async function loadPush() {
    try {
      const r = await apiFetch('/api/push-channels')
      if (r && r.success) push.value = r.data
    } catch (e) {
      /* 未登录或服务未就绪时静默：不影响持仓主数据 */
    }
  }

  function startEditPush() {
    pushMsg.value = ''
    editingPush.value = true
    loadPush()
    if (!bot.value.checked) refreshSessions() // 打开编辑器时才探测机器人，别在页面加载时打子进程
  }

  async function refreshSessions() {
    bot.value.busy = true
    try {
      const r = await apiFetch('/api/push-channels/discover')
      bot.value.checked = true
      bot.value.ok = !!r.success
      bot.value.error = r.success ? '' : r.error || r.detail || '查询失败'
      bot.value.sessions = (r.data && r.data.sessions) || []
    } catch (e) {
      bot.value.checked = true
      bot.value.ok = false
      bot.value.error = (e && e.detail) || '机器人查询失败'
    } finally {
      bot.value.busy = false
    }
  }

  async function searchBotUser() {
    const kw = (bot.value.keyword || '').trim()
    if (!kw) {
      pushMsgOk.value = false
      pushMsg.value = '请输入要搜索的姓名'
      return
    }
    bot.value.busy = true
    try {
      const r = await apiFetch('/api/push-channels/search-user', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ keyword: kw }),
      })
      bot.value.ok = !!r.success
      bot.value.error = r.success ? '' : r.error || r.detail || '搜索失败'
      bot.value.users = (r.data && r.data.users) || []
      if (r.success && !bot.value.users.length) {
        pushMsgOk.value = false
        pushMsg.value = '没搜到该姓名，或机器人没有通讯录权限'
      }
    } finally {
      bot.value.busy = false
    }
  }

  async function bindAibot(chatId, label) {
    if (!chatId) return
    const r = await pushPost('/api/push-channels/aibot', { chat_id: chatId, label: label || '' })
    if (r && r.success) {
      pushMsgOk.value = true
      pushMsg.value = '单聊确认消息已发出，请在企业微信里查收'
      agentInput.value = ''
      await loadPush()
    }
  }

  function bindAibotManual() {
    return bindAibot((agentInput.value || '').trim(), '')
  }

  async function pushPost(url, extra) {
    savingPush.value = true
    pushMsg.value = ''
    try {
      const r = await apiFetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(extra || {}),
      })
      if (!r.success) {
        pushMsgOk.value = false
        pushMsg.value = r.detail || r.message || '操作失败'
      }
      return r
    } catch (e) {
      pushMsgOk.value = false
      pushMsg.value = (e && e.detail) || '请求失败，请检查服务是否在运行'
      return null
    } finally {
      savingPush.value = false
    }
  }

  async function bindPush() {
    const url = (pushInput.value || '').trim()
    if (!url) {
      pushMsgOk.value = false
      pushMsg.value = '请先粘贴群机器人的 Webhook 地址'
      return
    }
    const r = await pushPost('/api/push-channels', { url, label: pushLabel.value.trim() })
    if (r && r.success) {
      pushMsgOk.value = true
      pushMsg.value = '已发送确认消息，请在企业微信里查收'
      pushInput.value = ''
      pushLabel.value = ''
      await loadPush()
    }
  }

  async function testPush(id) {
    const r = await pushPost(`/api/push-channels/${id}/test`)
    if (r && r.success) {
      pushMsgOk.value = true
      pushMsg.value = r.message
    }
  }

  async function unbindPush(id) {
    savingPush.value = true
    pushMsg.value = ''
    try {
      const r = await apiFetch(`/api/push-channels/${id}`, { method: 'DELETE' })
      pushMsgOk.value = !!r.success
      pushMsg.value = r.success ? '已解绑' : r.detail || '解绑失败'
      await loadPush()
    } finally {
      savingPush.value = false
    }
  }

  // ===== 加仓 / 减仓（2026-10-14）=====
  // 加权成本由服务端算（services/position_adjust 是唯一口径），这里只做一个预览，
  // 目的是让用户点「确认」之前就看到新成本、新股数和本次实现盈亏，而不是事后猜。
  const adj = ref({ symbol: '', mode: 'add', shares: '', price: '', saving: false })
  const adjSharesInput = ref(null)

  function startAdjust(p, mode) {
    adj.value = { symbol: p.symbol, mode, shares: '', price: String(p.price ?? ''), saving: false }
    nextTick(() => {
      // v-for 里的同名 ref 会被收集成数组，取第一个；单元素时也可能直接是 DOM 节点
      const el = Array.isArray(adjSharesInput.value) ? adjSharesInput.value[0] : adjSharesInput.value
      if (el && el.focus) el.focus()
    })
  }

  function cancelAdjust() {
    adj.value = { ...adj.value, symbol: '', mode: 'add', shares: '', price: '' }
  }

  function adjPreview(p) {
    const delta = parseInt(adj.value.shares) || 0
    const price = parseFloat(adj.value.price) || 0
    const oldShares = Number(p.shares) || 0
    const oldCost = Number(p.cost) || 0
    if (!delta || !price || !oldShares || !oldCost) return ''
    if (adj.value.mode === 'add') {
      const n = oldShares + delta
      return `改后 ${n} 股 @ ${((oldCost * oldShares + price * delta) / n).toFixed(3)}`
    }
    if (delta > oldShares) return `最多只能卖 ${oldShares} 股`
    const n = oldShares - delta
    const realized = (price - oldCost) * delta
    return `改后 ${n} 股 @ ${oldCost}（减仓不改成本）· 本次实现 ${realized >= 0 ? '+' : ''}${realized.toFixed(2)} 元`
  }

  async function submitAdjust(p) {
    const delta = parseInt(adj.value.shares) || 0
    const price = parseFloat(adj.value.price) || 0
    if (!delta) {
      showNotification('请输入股数', 'warning')
      return
    }
    if (!price) {
      showNotification('请输入成交价', 'warning')
      return
    }
    adj.value.saving = true
    try {
      const r = await apiFetch('/api/positions/adjust', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ symbol: p.symbol, mode: adj.value.mode, shares: delta, price }),
      })
      if (r.success) {
        showNotification(r.message || '已更新持仓', 'success', 3000)
        adj.value = { symbol: '', mode: 'add', shares: '', price: '', saving: false }
        await loadPositions()
      } else {
        showNotification(r.detail || '操作失败', 'error', 3000)
      }
    } catch (e) {
      showNotification('操作失败：' + (e?.message || e), 'error', 3000)
    } finally {
      adj.value.saving = false
    }
  }

  // ===== 初始本金（2026-10-14）=====
  // 仓位没有分母就没有意义：只持一只票时，"市值/持仓市值"永远是 100%。
  // 本金存在后端 user_capital 表，按账号隔离，只作为百分比的基数，不参与任何买卖信号判定。
  const editingCapital = ref(false)
  const capitalInput = ref('')
  const capitalInputRef = ref(null)

  function fmtMoney(v) {
    const n = Number(v || 0)
    const abs = Math.abs(n)
    if (abs >= 1e8) return `${(n / 1e8).toFixed(2)}亿`
    if (abs >= 1e4) return `${(n / 1e4).toFixed(abs >= 1e5 ? 1 : 2)}万`
    return `${n.toFixed(0)}元`
  }

  // 统一卖出计划的一句话摘要：触发就说触发哪条，没触发就把三条的当前线摆出来。
  function exitPlanText(p) {
    if (!p || !p.enabled) return ''
    if (p.hit) return `${p.action_label}：${p.reason}`
    const mechs = p.mechanisms || []
    const fixed = mechs.find((m) => m.id === 'fixed')
    const logic = mechs.find((m) => m.id === 'logic')
    const time = mechs.find((m) => m.id === 'time')
    const parts = []
    if (fixed?.available) parts.push(`固定线${fixed.line}`)
    if (logic?.available) parts.push(`MA20 ${logic.line}`)
    if (time?.available) parts.push(`持${p.daysHeld}天`)
    return `三机制未触发（${parts.join(' / ') || '数据不足'}）`
  }

  function startEditCapital() {
    capitalInput.value = data.value.portfolio?.capital_set ? String(data.value.portfolio.principal) : ''
    editingCapital.value = true
    nextTick(() => capitalInputRef.value && capitalInputRef.value.focus())
  }

  function cancelCapital() {
    editingCapital.value = false
    capitalInput.value = ''
  }

  async function saveCapital() {
    const raw = String(capitalInput.value || '').trim()
    if (raw === '') {
      showNotification('请输入本金金额（填 0 可清除）', 'warning')
      return
    }
    const v = Number(raw)
    if (!Number.isFinite(v) || v < 0) {
      showNotification('本金要是不小于 0 的数字', 'warning')
      return
    }
    saving.value = true
    try {
      const r = await apiFetch('/api/positions/capital', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ principal: v }),
      })
      if (r.success) {
        showNotification(r.message || '本金已保存', 'success')
        editingCapital.value = false
        await loadPositions()
      } else {
        showNotification(r.detail || '保存失败', 'error')
      }
    } catch (e) {
      showNotification('保存失败：' + (e?.message || e), 'error')
    } finally {
      saving.value = false
    }
  }

  function onStockQueryInput() {
    clearTimeout(searchTimer)
    const q = stockQuery.value.trim()
    if (!q) {
      searchResults.value = []
      return
    }
    searchTimer = setTimeout(async () => {
      searchLoading.value = true
      try {
        const r = await apiFetch(`/api/stock-search?q=${encodeURIComponent(q)}`)
        searchResults.value = r.success && r.data ? r.data : []
      } catch (e) {
        searchResults.value = []
      }
      searchLoading.value = false
    }, 300)
  }

  function selectStock(s) {
    form.value.symbol = s.symbol
    form.value.name = s.name
    stockQuery.value = `${s.name}（${s.symbol.toUpperCase()}）`
    currentPrice.value = s.price || null
    searchResults.value = []
  }

  function onDocClick(e) {
    if (!e.target.closest('.combo-wrap')) searchResults.value = []
  }

  async function submitPosition() {
    if (!stockQuery.value || !form.value.cost_price) {
      showNotification('请先选择股票并填写成本价', 'warning')
      return
    }
    saving.value = true
    try {
      const result = await apiFetch('/api/positions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          symbol: form.value.symbol || stockQuery.value,
          name: form.value.name,
          cost_price: parseFloat(form.value.cost_price),
          shares: parseInt(form.value.shares) || 100,
          buy_date: new Date().toISOString().slice(0, 10),
        }),
      })
      if (result.success) {
        showNotification(`持仓已保存：${result.data?.name || ''}`, 'success')
        resetForm()
        await loadPositions()
      } else {
        // 后端明确错误（代码格式/名称未匹配等）直接展示，不再"未知错误"
        const detail =
          typeof result.detail === 'string'
            ? result.detail
            : Array.isArray(result.detail)
              ? '请求格式有误，请检查输入'
              : result.error || '未知错误'
        showNotification('保存失败：' + detail, 'error')
      }
    } catch (e) {
      showNotification('保存失败：网络或服务异常', 'error')
    }
    saving.value = false
  }

  function editPosition(p) {
    editingSymbol.value = p.symbol
    form.value = { symbol: p.symbol, name: p.name, cost_price: p.cost, shares: p.shares }
    stockQuery.value = `${p.name}（${p.symbol.toUpperCase()}）`
    currentPrice.value = p.price || null
  }

  function resetForm() {
    editingSymbol.value = ''
    form.value = { symbol: '', name: '', cost_price: '', shares: '100' }
    stockQuery.value = ''
    currentPrice.value = null
    searchResults.value = []
  }

  async function removePosition(p) {
    if (!confirm(`确认删除持仓 ${p.name}(${p.symbol.toUpperCase()})？`)) return
    try {
      const result = await apiFetch(`/api/positions/${p.symbol}`, { method: 'DELETE' })
      if (result.success) {
        showNotification('已删除', 'success')
        await loadPositions()
      }
    } catch (e) {
      showNotification('删除失败', 'error')
    }
  }

  onMounted(() => {
    loadPositions()
    loadPush()
    document.addEventListener('click', onDocClick)
    startPositionPolling()
  })

  onUnmounted(() => {
    document.removeEventListener('click', onDocClick)
  })

  // 切换登录账号后，由 App 通过 ref 调用以立刻重载（否则会残留上一个账号的持仓）
  defineExpose({ loadPositions })
</script>

<style scoped>
  /* 交易环境横幅（2026-10-12） */
  .env-banner {
    margin-bottom: 1rem;
    border-radius: 12px;
    padding: 0.875rem 1rem;
    border: 1px solid;
  }
  .env-main {
    display: flex;
    align-items: center;
    gap: 0.875rem;
  }
  .env-emoji {
    font-size: 1.75rem;
    flex-shrink: 0;
  }
  .env-text {
    flex: 1;
    min-width: 0;
  }
  .env-title {
    font-size: 1rem;
    font-weight: 800;
    margin-bottom: 0.25rem;
  }
  .env-title small {
    font-weight: 400;
    font-size: 0.75rem;
    color: #64748b;
    margin-left: 0.5rem;
  }
  .env-detail {
    font-size: 0.8125rem;
    line-height: 1.6;
  }
  .env-notes {
    font-size: 0.75rem;
    color: #64748b;
    margin-top: 0.25rem;
  }
  .env-pct {
    flex-shrink: 0;
    text-align: center;
    font-size: 0.6875rem;
    color: #64748b;
    padding-left: 0.875rem;
    border-left: 1px dashed rgba(100, 116, 139, 0.3);
  }
  .env-pct b {
    font-size: 1.375rem;
  }
  .env-aggressive {
    background: #fef2f2;
    border-color: #fecaca;
  }
  .env-aggressive .env-title,
  .env-aggressive .env-pct b {
    color: #b91c1c;
  }
  .env-normal {
    background: #eff6ff;
    border-color: #bfdbfe;
  }
  .env-normal .env-title,
  .env-normal .env-pct b {
    color: #1d4ed8;
  }
  .env-cautious {
    background: #fff7ed;
    border-color: #fed7aa;
  }
  .env-cautious .env-title,
  .env-cautious .env-pct b {
    color: #c2410c;
  }
  .env-defensive {
    background: #fefce8;
    border-color: #fde68a;
  }
  .env-defensive .env-title,
  .env-defensive .env-pct b {
    color: #a16207;
  }
  .env-avoid {
    background: #fdf2f8;
    border-color: #fbcfe8;
  }
  .env-avoid .env-title,
  .env-avoid .env-pct b {
    color: #be185d;
  }
  .guide-box {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    margin-bottom: 1rem;
    overflow: hidden;
  }
  .guide-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 0.875rem 1rem;
    cursor: pointer;
    font-weight: 700;
    font-size: 0.9375rem;
    color: #334155;
    user-select: none;
  }
  .guide-toggle {
    font-size: 0.75rem;
    color: #94a3b8;
    font-weight: 400;
  }
  .guide-body {
    padding: 0 1rem 1rem;
  }
  .guide-section {
    margin-bottom: 0.875rem;
  }
  .guide-title {
    font-size: 0.875rem;
    font-weight: 700;
    color: #0f172a;
    margin-bottom: 0.375rem;
  }
  .guide-item {
    font-size: 0.8125rem;
    color: #475569;
    line-height: 1.7;
  }
  .guide-table {
    width: 100%;
    border-collapse: collapse;
    font-size: 0.75rem;
  }
  .guide-table th,
  .guide-table td {
    border: 1px solid #e2e8f0;
    padding: 0.375rem 0.5rem;
    text-align: left;
    color: #475569;
    line-height: 1.5;
  }
  .guide-table th {
    background: #f1f5f9;
    font-weight: 700;
    color: #334155;
  }
  .portfolio-box {
    background: linear-gradient(135deg, #eff6ff 0%, #dbeafe 100%);
    border: 1px solid #bfdbfe;
    border-radius: 12px;
    padding: 1rem;
    margin-bottom: 1rem;
  }
  .portfolio-row {
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
  }
  /* 本金行：与下面的组合指标用虚线分隔，视觉上属于同一张卡片但是"设置项"而非"结果项" */
  .capital-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.375rem 0.75rem;
    padding-bottom: 0.625rem;
    margin-bottom: 0.625rem;
    border-bottom: 1px dashed #bfdbfe;
  }
  .capital-label {
    font-size: 0.75rem;
    color: #64748b;
  }
  .capital-value {
    font-size: 1.125rem;
    color: #1e40af;
  }
  .capital-value.unset {
    font-size: 0.875rem;
    font-weight: 600;
    color: #94a3b8;
  }
  .capital-pct {
    font-size: 0.75rem;
    color: #64748b;
  }
  .capital-pct b {
    font-size: 1rem;
    color: #1e40af;
    margin: 0 0.1875rem;
  }
  .capital-pct small {
    font-size: 0.6875rem;
    color: #94a3b8;
  }
  .capital-free {
    font-size: 0.75rem;
    color: #64748b;
  }
  /* 当前资金 / 总盈亏（2026-10-23）：跟本金行同组，flex-basis 100% 独占一行，
     回答"清仓后我的钱变成多少"——初始本金是死数，已实现盈亏才是活的。 */
  .capital-sub-row {
    flex-basis: 100%;
    display: flex;
    flex-wrap: wrap;
    align-items: baseline;
    gap: 0.125rem 0.875rem;
    margin-top: 0.25rem;
    font-size: 0.75rem;
    color: #64748b;
    cursor: help;
  }
  .capital-sub-item b {
    font-size: 0.9375rem;
    margin: 0 0.125rem;
  }
  .capital-sub-item small {
    font-size: 0.6875rem;
    color: #94a3b8;
  }
  .capital-edit {
    font-size: 0.75rem;
    padding: 0.1875rem 0.625rem;
    border-radius: 999px;
    border: 1px solid #bfdbfe;
    background: #fff;
    color: #1e40af;
    cursor: pointer;
  }
  .capital-edit-wrap {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    width: 100%;
  }
  .capital-input {
    width: 9.5rem;
  }
  .capital-hint {
    font-size: 0.6875rem;
    color: #94a3b8;
    flex-basis: 100%;
  }

  /* ===== 企业微信推送通道（2026-10-14）=====
   默认只占一行（状态 + 一个按钮），点开才出现输入框和三步说明 ——
   持仓页要留给持仓本身，设置类东西收起来。 */
  .push-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    margin-top: 0.375rem;
    padding-top: 0.375rem;
    border-top: 1px dashed var(--border-subtle);
  }
  .push-label {
    font-size: 0.75rem;
    color: var(--text-tertiary);
  }
  .push-value {
    font-size: 0.8125rem;
    color: var(--text-primary);
  }
  .push-value.unset {
    color: #94a3b8;
    font-weight: 400;
  }
  .push-meta {
    font-size: 0.6875rem;
    color: var(--text-tertiary);
  }
  .push-meta small {
    margin-left: 0.25rem;
  }
  .push-edit-wrap {
    display: flex;
    flex-direction: column;
    gap: 0.375rem;
    flex-basis: 100%;
  }
  .push-input,
  .push-name-input {
    width: 100%;
    box-sizing: border-box;
    font-size: 0.75rem;
    padding: 0.375rem 0.5rem;
    border: 1px solid var(--border-medium);
    border-radius: var(--radius-sm);
    background: var(--bg-primary);
    color: var(--text-primary);
  }
  .push-name-input {
    max-width: 14rem;
  }
  .push-btns {
    display: flex;
    align-items: center;
    gap: 0.5rem;
  }
  .push-steps {
    margin: 0;
    padding-left: 1.125rem;
    font-size: 0.6875rem;
    line-height: 1.6;
    color: #94a3b8;
  }
  .push-list {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .push-item {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.375rem;
    font-size: 0.6875rem;
  }
  .push-item-name {
    color: var(--text-primary);
    font-weight: 600;
  }
  .push-item-url {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--text-tertiary);
    font-size: 0.625rem;
  }
  .push-mini {
    font-family: inherit;
    font-size: 0.6875rem;
    padding: 1px 8px;
    cursor: pointer;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    border: 1px solid var(--border-subtle);
    border-radius: var(--radius-pill);
  }
  .push-mini.danger {
    color: var(--accent-danger);
    border-color: var(--accent-danger);
  }
  .push-mini:disabled {
    opacity: 0.5;
    cursor: default;
  }
  .push-msg {
    font-size: 0.6875rem;
  }
  .push-msg.ok {
    color: var(--accent-success);
  }
  .push-msg.bad {
    color: var(--accent-danger);
  }
  .push-agent {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.375rem;
    margin-top: 0.25rem;
    padding-top: 0.375rem;
    border-top: 1px dashed var(--border-subtle);
  }
  .push-agent-title {
    font-size: 0.6875rem;
    color: var(--text-secondary);
    font-weight: 600;
  }
  .push-agent-state {
    font-size: 0.6875rem;
    color: #94a3b8;
  }
  .push-agent-state.ok {
    color: var(--accent-success);
  }
  .push-agent-hint {
    font-size: 0.625rem;
    line-height: 1.5;
    color: #94a3b8;
    flex-basis: 100%;
  }
  .portfolio-metric {
    display: flex;
    flex-direction: column;
  }
  .portfolio-metric em {
    font-style: normal;
    font-size: 0.75rem;
    color: #64748b;
    margin-bottom: 0.125rem;
  }
  .portfolio-metric b {
    font-size: 1.125rem;
  }
  .portfolio-metric small {
    font-size: 0.6875rem;
    color: #64748b;
    margin-top: 0.125rem;
  }
  .portfolio-warnings {
    margin-top: 0.625rem;
    display: flex;
    flex-wrap: wrap;
    gap: 0.375rem;
  }
  .warn-chip {
    font-size: 0.75rem;
    color: #b45309;
    background: #fef3c7;
    border: 1px solid #fde68a;
    border-radius: 999px;
    padding: 0.125rem 0.5rem;
  }
  .portfolio-updated {
    margin-top: 0.5rem;
    font-size: 0.6875rem;
    color: #94a3b8;
  }
  .pos-form-box {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 12px;
    padding: 0.875rem 1rem;
    margin-bottom: 1rem;
  }
  .form-title {
    font-size: 0.875rem;
    font-weight: 700;
    color: #334155;
    margin-bottom: 0.5rem;
  }
  .form-grid {
    display: grid;
    grid-template-columns: 2fr 1fr 1fr 1fr auto;
    gap: 0.625rem;
    align-items: end;
  }
  .form-field {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .form-field.wide {
    grid-column: span 1;
  }
  .form-label {
    font-size: 0.75rem;
    font-weight: 600;
    color: #64748b;
  }
  .form-input {
    padding: 0.5rem 0.75rem;
    border: 1px solid #cbd5e1;
    border-radius: 8px;
    font-size: 0.875rem;
    background: #fff;
    width: 100%;
    box-sizing: border-box;
  }
  .form-input:focus {
    outline: none;
    border-color: #3b82f6;
  }
  .form-input.readonly {
    background: #f1f5f9;
    color: #64748b;
    font-weight: 700;
    min-height: 35px;
    display: flex;
    align-items: center;
  }
  /* 股票智能搜索 combobox */
  .combo-wrap {
    position: relative;
  }
  .combo-dropdown {
    position: absolute;
    top: calc(100% + 4px);
    left: 0;
    right: 0;
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(15, 23, 42, 0.12);
    z-index: 20;
    max-height: 260px;
    overflow-y: auto;
  }
  .combo-item {
    display: flex;
    align-items: baseline;
    gap: 0.5rem;
    padding: 0.5rem 0.75rem;
    cursor: pointer;
    font-size: 0.8125rem;
  }
  .combo-item:hover {
    background: #f1f5f9;
  }
  .combo-item.combo-searching {
    color: #94a3b8;
    cursor: default;
  }
  .combo-name {
    color: var(--text-primary);
  }
  .combo-code {
    color: #94a3b8;
    font-size: 0.75rem;
  }
  .combo-price {
    margin-left: auto;
    color: var(--accent-danger);
    font-size: 0.75rem;
    font-weight: 700;
  }
  .form-actions {
    display: flex;
    gap: 0.375rem;
  }
  @media (max-width: 900px) {
    .form-grid {
      grid-template-columns: 1fr 1fr;
    }
  }
  .empty-box {
    text-align: center;
    padding: 3rem 2rem;
    color: #94a3b8;
    background: #f8fafc;
    border-radius: 16px;
    border: 2px dashed #e2e8f0;
  }
  .pos-table {
    display: flex;
    flex-direction: column;
    gap: 0.5rem;
  }
  .pos-table-header,
  .pos-table-row {
    display: grid;
    grid-template-columns: 1.1fr 0.9fr 1fr 0.9fr 0.8fr 1.2fr 2fr 0.9fr;
    gap: 0.5rem;
    align-items: center;
    font-size: 0.8125rem;
  }
  .pos-table-header {
    font-weight: 700;
    color: #64748b;
    padding: 0.5rem 0.75rem;
    background: #f1f5f9;
    border-radius: 8px;
  }
  .pos-table-row {
    padding: 0.75rem;
    background: #fff;
    border: 1px solid #e2e8f0;
    border-radius: 10px;
  }
  .pos-name b {
    display: block;
    color: var(--text-primary);
  }
  .pos-name small {
    color: var(--text-tertiary);
    font-size: 0.6875rem;
  }
  .pos-price small,
  .pos-pnl small,
  .pos-stop small {
    font-size: 0.6875rem;
    color: #94a3b8;
  }
  .pos-stop {
    color: var(--accent-success);
    font-weight: 700;
  }
  .pos-target {
    color: var(--accent-danger);
    font-weight: 700;
  }
  .pos-flags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.25rem;
  }
  .flag-chip {
    font-size: 0.6875rem;
    padding: 0.0625rem 0.375rem;
    border-radius: 999px;
    background: #fef2f2;
    color: #b91c1c;
    border: 1px solid #fecaca;
  }
  .flag-chip.ok {
    background: #f0fdf4;
    color: #15803d;
    border-color: #bbf7d0;
  }
  .pos-action {
    display: flex;
    flex-direction: column;
    gap: 0.25rem;
  }
  .action-badge {
    display: inline-block;
    width: fit-content;
    font-size: 0.75rem;
    font-weight: 700;
    padding: 0.1875rem 0.5rem;
    border-radius: 6px;
    border: 1px solid transparent;
  }
  .action-stop_loss {
    background: #fef2f2;
    color: #b91c1c;
    border-color: #fecaca;
  }
  .action-take_profit {
    background: #fff1f2;
    color: #be123c;
    border-color: #fecdd3;
  }
  .action-reduce {
    background: #fff7ed;
    color: #c2410c;
    border-color: #fed7aa;
  }
  /* 冲高回落：比「减仓回避」轻一档、比「持有观察」重一档，用琥珀黄，颜色与行动台徽章保持一致 */
  .action-giveback {
    background: #fef9c3;
    color: #854d0e;
    border-color: #fde047;
  }
  .action-hold_trailing {
    background: #eff6ff;
    color: #1d4ed8;
    border-color: #bfdbfe;
  }
  .action-hold {
    background: #f8fafc;
    color: #475569;
    border-color: #e2e8f0;
  }
  .action-watch {
    background: #fefce8;
    color: #a16207;
    border-color: #fde68a;
  }
  .pos-detail {
    font-size: 0.6875rem;
    color: #64748b;
    line-height: 1.4;
  }
  .exit-plan {
    font-size: 0.6875rem;
    color: #94a3b8;
    line-height: 1.4;
  }
  .exit-plan.hit {
    color: #b45309;
    font-weight: 600;
  }
  .pos-ops {
    display: flex;
    gap: 0.25rem;
    justify-content: center;
  }
  .op-btn {
    font-size: 0.75rem;
    padding: 0.25rem 0.5rem;
    border: 1px solid #cbd5e1;
    background: #fff;
    border-radius: 6px;
    cursor: pointer;
    color: #475569;
  }
  .op-btn:hover {
    background: #f1f5f9;
  }
  .op-btn.danger {
    color: #b91c1c;
    border-color: #fecaca;
  }
  .op-btn.danger:hover {
    background: #fef2f2;
  }
  /* 四个按钮在一列里放不下就换行，不靠缩字号硬塞（手机上"加仓/减仓/编辑/删除"各占一行也可点） */
  .pos-ops {
    flex-wrap: wrap;
  }
  /* 加减仓展开条：紧跟在对应持仓行下面，用虚线框表示"这是临时输入区" */
  .adj-bar {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 0.5rem;
    padding: 0.625rem 0.75rem;
    background: #f8fafc;
    border: 1px dashed #cbd5e1;
    border-radius: 10px;
  }
  .adj-title {
    font-size: 0.8125rem;
    font-weight: 700;
    color: var(--text-primary);
  }
  .adj-now {
    font-size: 0.75rem;
    color: var(--text-secondary);
  }
  .adj-input {
    width: 7rem;
  }
  /* 预览：确认前就把新成本/新盈亏说出来，省掉"提交完才发现算错" */
  .adj-preview {
    font-size: 0.75rem;
    font-weight: 700;
    color: #1d4ed8;
  }
  .adj-note-line {
    flex-basis: 100%;
    font-size: 0.6875rem;
    color: #b45309;
  }
  /* 窄窗口治理（2026-10-13）
   原实现在 ≤900px 时把表头 display:none、把行压成两列 —— 屏幕窄了不等于数据可以没有标题，
   那串无标题数字对上班族是最容易看错的一种（止损价和目标价长得几乎一样）。
   现在：900px 以下用横向滚动保留完整表格；只有窄到手机级（≤620px）才转成卡片，
   卡片里用 ::before 把每一格的列标题补回来。 */
  @media (max-width: 980px) {
    .pos-table {
      overflow-x: auto;
      -webkit-overflow-scrolling: touch;
      padding-bottom: 4px;
    }
    .pos-table-header,
    .pos-table-row {
      min-width: 760px;
    }
  }

  @media (max-width: 620px) {
    .pos-table {
      overflow-x: visible;
    }
    .pos-table-header {
      display: none;
    }
    .pos-table-row {
      grid-template-columns: 1fr 1fr;
      min-width: 0;
      gap: 0.625rem 0.75rem;
    }
    .pos-table-row > span {
      display: flex;
      flex-direction: column;
      gap: 2px;
    }
    .pos-table-row > span::before {
      font-size: 0.6875rem;
      color: #94a3b8;
      letter-spacing: 0.02em;
      margin-bottom: 1px;
    }
    .pos-name::before {
      content: '名称';
    }
    .pos-price::before {
      content: '成本 / 现价';
    }
    .pos-pnl::before {
      content: '盈亏';
    }
    .pos-stop::before {
      content: '止损价';
    }
    .pos-target::before {
      content: '目标价';
    }
    .pos-flags::before {
      content: '信号';
    }
    .pos-action::before {
      content: '建议';
    }
    .pos-ops::before {
      content: '操作';
    }
    .pos-name,
    .pos-action,
    .pos-ops {
      grid-column: 1 / -1;
    }
    /* 加仓/减仓/编辑/删除四个按钮在手机上排成 2×2：竖着堆四行会把操作区撑得比票本身还高。
     选择器必须带 .pos-table-row > 前缀 —— 上面那条 `.pos-table-row > span` 特异性更高，
     只写 .pos-ops 会被它的 display:flex 压住（第一次就这样，改完才生效）。 */
    .pos-table-row > .pos-ops {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 0.375rem;
    }
    /* 「操作」这个列标题是 ::before 生成的，它也会占一格 —— 不跨行的话按钮就被顶成
     [空][加仓] / [减仓][编辑] / [删除] 的错位排布。让它单独占一行头。 */
    .pos-table-row > .pos-ops::before {
      grid-column: 1 / -1;
    }
  }
</style>
