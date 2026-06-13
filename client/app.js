let autoRefreshInterval = null;
let currentSymbol = 'sh603985';

let klineChartInstance = null;
let klineAutoRefreshInterval = null;
let currentKlineSymbol = null;
let currentKlineType = 'day';


// 判断是否为交易时间
// 详细定义见文件下方统一定义（含周末/节假日判断）：
//   盘中：交易日 09:30-11:30 和 13:00-15:00
//   盘后：交易日 15:00 之后，以及所有非交易日（周末、节假日）


// 获取下次刷新的等待时间
function getNextRefreshDelay() {
    if (isTradingTime()) {
        return 1000; // 交易时间1秒刷新
    }
    
    // 非交易时间，计算到下次交易的等待时间
    const now = new Date();
    const day = now.getDay();
    const hours = now.getHours();
    const minutes = now.getMinutes();
    const seconds = now.getSeconds();
    
    // 下次开始交易的时间
    let nextTradingStart = new Date(now);
    
    // 周末：等到周一9:30
    if (day === 0 || day === 6) {
        const daysToMonday = day === 0 ? 1 : 2;
        nextTradingStart.setDate(now.getDate() + daysToMonday);
        nextTradingStart.setHours(9, 30, 0, 0);
    } 
    // 工作日但在15:00之后：等明天9:30
    else if (hours >= 15) {
        nextTradingStart.setDate(now.getDate() + 1);
        nextTradingStart.setHours(9, 30, 0, 0);
    }
    // 工作日在11:30-13:00之间：等13:00
    else if (hours >= 11 && (hours < 13 || (hours === 12 && minutes >= 30))) {
        nextTradingStart.setHours(13, 0, 0, 0);
    }
    // 工作日在9:30之前：等今天9:30
    else {
        nextTradingStart.setHours(9, 30, 0, 0);
    }
    
    const delay = nextTradingStart.getTime() - now.getTime();
    return Math.max(delay, 60000); // 至少1分钟，避免负数
}

let autoScanInterval = null;
let hasScanResult = false;

// 智能扫描刷新调度函数
function scheduleScanRefresh() {
    stopAutoScan();
    
    const refreshOnce = async () => {
        try {
            if (!isScanning) {
                await startScan(true);
            }
        } catch (e) {
            console.error('自动扫描失败:', e);
        }
        
        // 计算下一次刷新时间
        const nextDelay = isTradingTime() ? 5000 : getNextRefreshDelay();
        autoScanInterval = setTimeout(refreshOnce, nextDelay);
    };
    
    // 立即开始第一次扫描（静默）
    refreshOnce();
}

// K线图表相关

function openKlineModal(symbol) {
    const modal = document.getElementById('klineModal');
    const title = document.getElementById('klineModalTitle');
    modal.style.display = 'flex';
    
    if (!klineChartInstance) {
        const chartDom = document.getElementById('klineChart');
        klineChartInstance = echarts.init(chartDom);
        window.addEventListener('resize', () => klineChartInstance.resize());
    }
    
    currentKlineSymbol = symbol;
    currentKlineType = 'day';
    
    document.querySelectorAll('.kline-type-btn').forEach(b => b.classList.remove('active'));
    document.querySelector('.kline-type-btn[data-type="day"]').classList.add('active');
    
    fetchKlineData(symbol);
    scheduleKlineRefresh();
}

function closeKlineModal() {
    const modal = document.getElementById('klineModal');
    modal.style.display = 'none';
    stopKlineRefresh();
}

function switchKlineType(type) {
    currentKlineType = type;
    
    document.querySelectorAll('.kline-type-btn').forEach(b => b.classList.remove('active'));
    document.querySelector(`.kline-type-btn[data-type="${type}"]`).classList.add('active');
    
    if (type === 'day') {
        fetchKlineData(currentKlineSymbol);
    } else {
        fetchMinuteKlineData(currentKlineSymbol);
    }
    
    scheduleKlineRefresh();
}

// K线图智能刷新调度函数
function scheduleKlineRefresh() {
    stopKlineRefresh();
    
    const refreshOnce = async () => {
        try {
            if (currentKlineSymbol) {
                if (currentKlineType === 'minute') {
                    await fetchMinuteKlineData(currentKlineSymbol);
                } else {
                    await fetchKlineData(currentKlineSymbol);
                }
            }
        } catch (e) {
            console.error('K线自动刷新失败:', e);
        }
        
        const nextDelay = isTradingTime() ? 5000 : getNextRefreshDelay();
        klineAutoRefreshInterval = setTimeout(refreshOnce, nextDelay);
    };
    
    if (isTradingTime()) {
        refreshOnce();
    }
}

function stopKlineRefresh() {
    if (klineAutoRefreshInterval) {
        clearTimeout(klineAutoRefreshInterval);
        klineAutoRefreshInterval = null;
    }
}

async function fetchKlineData(symbol) {
    try {
        const resp = await fetch(`/api/kline/${symbol}`);
        const json = await resp.json();
        if (json.success && json.data) {
            renderKlineChart(json.data);
        } else {
            showNotification(json.error || '获取K线数据失败');
        }
    } catch (e) {
        console.error('[K线] 错误:', e);
        showNotification('获取K线数据失败');
    }
}

async function fetchMinuteKlineData(symbol) {
    try {
        const resp = await fetch(`/api/kline/${symbol}/minute`);
        const json = await resp.json();
        if (json.success && json.data) {
            renderMinuteChart(json.data);
        } else {
            showNotification(json.error || '获取分时数据失败');
        }
    } catch (e) {
        console.error('[分时] 错误:', e);
        showNotification('获取分时数据失败');
    }
}

function renderMinuteChart(data) {
    const title = document.getElementById('klineModalTitle');
    title.textContent = `分时图 - ${data.name} (${data.symbol})`;

    const upColor = '#ef4444';
    const downColor = '#22c55e';

    const option = {
        backgroundColor: '#fff',
        animation: false,
        legend: {
            top: 10,
            data: ['价格', '昨收'],
            left: 'center'
        },
        tooltip: {
            trigger: 'axis',
            axisPointer: { type: 'cross' },
            backgroundColor: 'rgba(248, 250, 252, 0.95)',
            borderColor: '#e2e8f0',
            borderWidth: 1,
            textStyle: { color: '#1e293b' }
        },
        grid: [
            { left: '10%', right: '8%', top: 70, height: '55%' },
            { left: '10%', right: '8%', top: '68%', height: '14%' }
        ],
        xAxis: [
            { type: 'category', data: data.times, axisLine: { onZero: false }, boundaryGap: false, axisLabel: { rotate: 45, fontSize: 10 } },
            { type: 'category', gridIndex: 1, data: data.times, axisLine: { onZero: false }, axisLabel: { show: false }, boundaryGap: false }
        ],
        yAxis: [
            { scale: true, splitArea: { show: true }, min: function(val) { return (val.min - (val.max - val.min) * 0.05).toFixed(2); }, max: function(val) { return (val.max + (val.max - val.min) * 0.05).toFixed(2); } },
            { scale: true, gridIndex: 1, splitNumber: 3, axisLabel: { show: true } }
        ],
        series: [
            {
                name: '价格',
                type: 'line',
                data: data.prices,
                smooth: false,
                symbol: 'none',
                lineStyle: { width: 1.5, color: '#1e3a8a' },
                areaStyle: {
                    color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
                        { offset: 0, color: 'rgba(30, 58, 138, 0.3)' },
                        { offset: 1, color: 'rgba(30, 58, 138, 0.02)' }
                    ])
                }
            },
            {
                name: '昨收',
                type: 'line',
                data: new Array(data.times.length).fill(data.prevClose),
                symbol: 'none',
                lineStyle: { width: 1, color: '#94a3b8', type: 'dashed' }
            },
            {
                name: '成交量',
                type: 'bar',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: data.vols,
                itemStyle: {
                    color: function(params) {
                        const idx = params.dataIndex;
                        if (idx === 0) return upColor;
                        return data.prices[idx] >= data.prices[idx - 1] ? upColor : downColor;
                    }
                }
            }
        ]
    };
    klineChartInstance.setOption(option, true);
}

function renderKlineChart(data) {
    const title = document.getElementById('klineModalTitle');
    title.textContent = `日K线 - ${data.name} (${data.symbol})`;

    const upColor = '#ef4444';
    const downColor = '#22c55e';

    const option = {
        backgroundColor: '#fff',
        animation: false,
        legend: {
            top: 10,
            data: ['日K', 'MA5', 'MA10', 'MA20'],
            left: 'center'
        },
        tooltip: {
            trigger: 'axis',
            axisPointer: {
                type: 'cross'
            },
            backgroundColor: 'rgba(248, 250, 252, 0.95)',
            borderColor: '#e2e8f0',
            borderWidth: 1,
            textStyle: {
                color: '#1e293b'
            }
        },
        grid: [
            { left: '10%', right: '8%', top: 70, height: '50%' },
            { left: '10%', right: '8%', top: '63%', height: '16%' }
        ],
        xAxis: [
            { type: 'category', data: data.dates, axisLine: { onZero: false }, boundaryGap: true, axisLabel: { formatter: function(value) { return value.slice(5); } } },
            { type: 'category', gridIndex: 1, data: data.dates, axisLine: { onZero: false }, boundaryGap: true, axisLabel: { show: false } }
        ],
        yAxis: [
            { scale: true, splitArea: { show: true } },
            { scale: true, gridIndex: 1, splitNumber: 2, axisLabel: { show: true } }
        ],
        dataZoom: [
            { type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, data.dates.length - 100) * 100 / data.dates.length, end: 100 },
            { show: true, xAxisIndex: [0, 1], type: 'slider', bottom: 10, start: Math.max(0, data.dates.length - 100) * 100 / data.dates.length, end: 100 }
        ],
        series: [
            {
                name: '日K',
                type: 'candlestick',
                data: data.values,
                itemStyle: {
                    color: upColor,
                    color0: downColor,
                    borderColor: upColor,
                    borderColor0: downColor
                }
            },
            { name: 'MA5', type: 'line', data: data.ma5, smooth: true, symbol: 'none', lineStyle: { width: 1 }, color: '#f59e0b' },
            { name: 'MA10', type: 'line', data: data.ma10, smooth: true, symbol: 'none', lineStyle: { width: 1 }, color: '#8b5cf6' },
            { name: 'MA20', type: 'line', data: data.ma20, smooth: true, symbol: 'none', lineStyle: { width: 1 }, color: '#06b6d4' },
            {
                name: '成交量',
                type: 'bar',
                xAxisIndex: 1,
                yAxisIndex: 1,
                data: data.vols,
                itemStyle: {
                    color: function(params) {
                        const idx = params.dataIndex;
                        const val = data.values[idx];
                        return val[1] > val[0] ? upColor : downColor;
                    }
                }
            }
        ]
    };
    klineChartInstance.setOption(option, true);
}

// 从配置文件获取禁止关键词（如果window上没有定义才设置兜底）
if (typeof window !== 'undefined' && !window.FORBIDDEN_KEYWORDS) {
    window.FORBIDDEN_KEYWORDS = ['银行', '酒', '证券', '保险', '家电', '地产', '铁路', '航运', '乳业', '物流', '运输', '快递', '游戏', '影视'];
}

// 检查是否为禁止添加的股票
function isForbiddenStock(name) {
    if (!name) return false;
    const keywords = window.FORBIDDEN_KEYWORDS || [];
    return keywords.some(keyword => name.includes(keyword));
}

// 从API加载热门股票
let hotStocksCache = [];

async function loadHotStocksFromAPI() {
    try {
        const response = await fetch('/api/hot-stocks');
        const result = await response.json();
        if (result.success && result.data && result.data.stocks) {
            hotStocksCache = result.data.stocks;
            // 更新全局变量
            window.quickStocks = hotStocksCache;
            return hotStocksCache;
        }
    } catch (error) {
        console.error('加载热门股票失败:', error);
    }
    return getQuickStocks(); // 失败时回退到原始配置
}

// 添加股票到热门股票
async function addStockToHotStocks(code, name) {
    try {
        const response = await fetch('/api/hot-stocks/add', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code, name })
        });
        const result = await response.json();
        if (result.success) {
            hotStocksCache = result.data.stocks;
            window.quickStocks = hotStocksCache;
            await initQuickButtons();
            return { success: true };
        } else {
            return { success: false, error: result.error || '添加失败' };
        }
    } catch (error) {
        console.error('添加热门股票失败:', error);
        return { success: false, error: '网络错误' };
    }
}

// 从热门股票删除
async function removeStockFromHotStocks(code) {
    try {
        const response = await fetch('/api/hot-stocks/remove', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ code })
        });
        const result = await response.json();
        if (result.success) {
            hotStocksCache = result.data.stocks;
            window.quickStocks = hotStocksCache;
            await initQuickButtons();
            return { success: true };
        }
    } catch (error) {
        console.error('删除热门股票失败:', error);
    }
    return { success: false, error: '删除失败' };
}

// 处理添加到热门股票
async function handleAddToHotStocks(code, name) {
    const btn = document.getElementById('addToHotBtn');
    if (btn) {
        btn.disabled = true;
        btn.textContent = '添加中...';
    }
    
    const result = await addStockToHotStocks(code, name);
    
    if (btn) {
        if (result.success) {
            btn.textContent = '✅ 已添加';
            setTimeout(() => {
                updateAddToHotBtn(code);
            }, 1500);
        } else {
            alert(result.error || '添加失败');
            btn.disabled = false;
            btn.textContent = '⭐ 添加到热门股票';
        }
    }
}

// 更新添加到热门按钮状态
function updateAddToHotBtn(code) {
    const btn = document.getElementById('addToHotBtn');
    if (!btn) return;
    
    const isInHot = hotStocksCache.some(s => s.code.toLowerCase() === code.toLowerCase());
    if (isInHot) {
        btn.disabled = true;
        btn.textContent = '✅ 已在热门股票';
    } else {
        btn.disabled = false;
        btn.textContent = '⭐ 添加到热门股票';
    }
}

// 获取股票实时股价
function fetchStockPrice(code) {
    return new Promise((resolve) => {
        const formattedSymbol = formatTencentSymbol(code);
        fetch(`/api/stock/${formattedSymbol}`)
            .then(response => response.text())
            .then(data => {
                const parsed = parseTencentData(data);
                resolve(parsed ? parseFloat(parsed.price) || 0 : 0);
            })
            .catch(() => resolve(0));
    });
}

// 动态生成快捷按钮
async function initQuickButtons() {
    const container = document.querySelector('.quick-buttons');
    let stocks = hotStocksCache.length > 0 ? hotStocksCache : await loadHotStocksFromAPI();
    
    if (container) {
        if (stocks.length > 0) {
            // 获取所有股票的实时股价
            const stocksWithPrice = await Promise.all(
                stocks.map(async (stock) => {
                    const price = await fetchStockPrice(stock.code);
                    return { ...stock, price };
                })
            );
            
            // 按照股价从高到低排序
            stocksWithPrice.sort((a, b) => b.price - a.price);
            
            container.innerHTML = stocksWithPrice.map(stock => 
                `<div class="quick-btn-wrapper" data-code="${stock.code}" data-name="${stock.name}">
                    <button class="quick-btn" data-code="${stock.code}" draggable="false">${stock.name}</button>
                    <button class="quick-btn-remove" data-code="${stock.code}" title="删除">×</button>
                </div>`
            ).join('');
            
            // 初始化拖拽排序
            if (window.Sortable) {
                // 如果已经存在实例，先销毁
                if (container._sortableInstance) {
                    container._sortableInstance.destroy();
                }
                
                container._sortableInstance = new Sortable(container, {
                    animation: 200, // 稍微增加动画时间，视觉更平滑
                    ghostClass: 'sortable-ghost',
                    chosenClass: 'sortable-chosen',
                    dragClass: 'sortable-drag',
                    forceFallback: true,
                    fallbackTolerance: 3,
                    delay: 100, // 进一步缩短延迟
                    delayOnTouchOnly: true,
                    touchStartThreshold: 5,
                    swapThreshold: 0.65, // 增加触发交换的阈值
                    invertSwap: true, // 启用反向交换逻辑
                    direction: 'horizontal', // 明确指定是水平流式布局
                    draggable: '.quick-btn-wrapper',
                    onEnd: async function() {
                        const newStocks = [];
                        container.querySelectorAll('.quick-btn-wrapper').forEach(el => {
                            newStocks.push({
                                code: el.getAttribute('data-code'),
                                name: el.getAttribute('data-name')
                            });
                        });
                        
                        // 保存新顺序到后端
                        try {
                            const response = await fetch('/api/hot-stocks', {
                                method: 'POST',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ stocks: newStocks })
                            });
                            const result = await response.json();
                            if (result.success) {
                                hotStocksCache = result.data.stocks;
                                window.quickStocks = hotStocksCache;
                                console.log('热门股票顺序已保存');
                            }
                        } catch (error) {
                            console.error('保存热门股票顺序失败:', error);
                        }
                    }
                });
            }
            
            // 绑定点击查看事件
            container.querySelectorAll('.quick-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const code = btn.getAttribute('data-code');
                    document.getElementById('stockSymbol').value = code;
                    currentSymbol = code;
                    fetchStockQuote(code, false);
                    updateLastUpdateTime();
                });
            });
            
            // 绑定删除事件
            container.querySelectorAll('.quick-btn-remove').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    const code = btn.getAttribute('data-code');
                    if (confirm('确定要删除这只股票吗？')) {
                        await removeStockFromHotStocks(code);
                    }
                });
            });
        } else {
            container.innerHTML = '<div style="color:#94a3b8;font-size:0.85rem;padding:0.5rem;">暂无热门股票</div>';
        }
    }
}

// 从window获取配置变量
function getQuickStocks() { return window.quickStocks || []; }

// 获取 名称 -> 代码 的映射 (用于搜索)
function getStockNameMap() { 
    const nameMap = {};
    
    // 1. 从 stocks-config.js 的原始 stockNameMap 获取
    if (typeof stockNameMap !== 'undefined') {
        Object.assign(nameMap, stockNameMap);
    }
    
    // 2. 从 window.stockSymbolMap 获取 (由 loadStockMapFromServer 构建)
    if (window.stockSymbolMap) {
        Object.assign(nameMap, window.stockSymbolMap);
    }
    
    return nameMap;
}

// 获取 代码 -> 名称 的映射 (用于显示)
function getStockCodeMap() {
    const codeMap = {};
    
    // 1. 从 quickStocks 获取
    getQuickStocks().forEach(s => {
        codeMap[s.code.toLowerCase()] = s.name;
    });
    
    // 2. 从 stocks-config.js 的原始 stockNameMap 反转获取
    if (typeof stockNameMap !== 'undefined') {
        for (const [name, code] of Object.entries(stockNameMap)) {
            if (/^(sh|sz)\d{6}$/i.test(code)) {
                codeMap[code.toLowerCase()] = name;
            }
        }
    }
    
    // 3. 从 window.stockNameMap 获取 (由 loadStockMapFromServer 加载的 Code -> Name)
    if (window.stockNameMap) {
        for (const [key, value] of Object.entries(window.stockNameMap)) {
            if (/^(sh|sz)\d{6}$/i.test(key)) {
                codeMap[key.toLowerCase()] = value;
            }
        }
    }
    
    return codeMap;
}
function getScanStockPool() { return window.scanStockPool || []; }
function getStockTags() { return window.stockTags || {}; }

// 从服务器加载股票名称映射
async function loadStockMapFromServer() {
    try {
        const response = await fetch('/api/stock-map');
        const result = await response.json();
        if (result.success && result.data) {
            // filteredData 是从服务器获取的 Code -> Name 映射
            const filteredData = {};
            const reverseMap = {};
            
            for (const [symbol, name] of Object.entries(result.data)) {
                if (!isForbiddenStock(name)) {
                    filteredData[symbol.toLowerCase()] = name;
                    reverseMap[name] = symbol.toLowerCase();
                }
            }
            
            // 保存到全局变量
            window.stockNameMap = filteredData; // Code -> Name
            
            // 合并数据库中的名称搜索映射（nameSearch: 名称 -> 代码）
            if (result.nameSearch) {
                for (const [name, symbol] of Object.entries(result.nameSearch)) {
                    reverseMap[name] = symbol.toLowerCase();
                }
            }
            window.stockSymbolMap = reverseMap; // Name -> Code（含简称）
            return true;
        }
    } catch (error) {
        console.error('加载股票映射失败:', error);
    }
    return false;
}

function renderStockTags(symbol) {
    const tags = getStockTags()[symbol];
    if (!tags || tags.length === 0) return '';
    const tagColors = ['blue', 'green', 'orange', 'purple', 'red'];
    let html = '<div class="stock-tags">';
    tags.forEach((tag, index) => {
        const colorClass = tagColors[index % tagColors.length];
        html += `<span class="stock-tag ${colorClass}">${escapeHtml(tag)}</span>`;
    });
    html += '</div>';
    return html;
}

function renderScanTags(symbol) {
    const tags = getStockTags()[symbol];
    if (!tags || tags.length === 0) return '';
    const tagColors = ['blue', 'green', 'orange', 'purple', 'red'];
    const bgMap = { blue: 'rgba(59,130,246,0.1)', green: 'rgba(16,185,129,0.1)', orange: 'rgba(251,146,60,0.1)', purple: 'rgba(168,85,247,0.1)', red: 'rgba(239,68,68,0.1)' };
    const colorMap = { blue: '#1d4ed8', green: '#047857', orange: '#c2410c', purple: '#7c3aed', red: '#dc2626' };
    let html = '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px;">';
    tags.slice(0, 2).forEach((tag, index) => {
        const cls = tagColors[index % tagColors.length];
        html += `<span style="font-size:0.65rem;padding:2px 6px;border-radius:4px;background:${bgMap[cls]};color:${colorMap[cls]};">${escapeHtml(tag)}</span>`;
    });
    html += '</div>';
    return html;
}

const stockInput = document.getElementById('stockSymbol');
const fetchBtn = document.getElementById('fetchBtn');
const refreshNowBtn = document.getElementById('refreshNowBtn');
const autoRefreshToggle = document.getElementById('autoRefreshToggle');
const lastUpdateTimeSpan = document.getElementById('lastUpdateTime');
const quoteContainer = document.getElementById('quoteContainer');

function formatNowTime() {
    const now = new Date();
    return now.toLocaleTimeString('zh-CN', { hour12: false });
}

function updateLastUpdateTime() {
    lastUpdateTimeSpan.textContent = formatNowTime();
}

function formatTencentSymbol(symbol) {
    symbol = symbol.trim();
    const nameMap = getStockNameMap();
    
    if (nameMap[symbol]) {
        return nameMap[symbol];
    }
    
    const lowerSymbol = symbol.toLowerCase();
    
    if (lowerSymbol.startsWith('sh') || lowerSymbol.startsWith('sz')) {
        return lowerSymbol;
    }
    
    if (!isNaN(symbol) && symbol.length === 6) {
        if (symbol.startsWith('6')) {
            return 'sh' + symbol;
        } else if (symbol.startsWith('0') || symbol.startsWith('3')) {
            return 'sz' + symbol;
        } else {
            return 'sz' + symbol;
        }
    }
    
    return symbol;
}

function formatNumber(numStr) {
    if (!numStr) return '0';
    const num = parseFloat(numStr);
    if (num >= 100000000) {
        return (num / 100000000).toFixed(2) + '亿';
    } else if (num >= 10000) {
        return (num / 10000).toFixed(2) + '万';
    }
    return num.toLocaleString();
}

// 解析腾讯财经API返回的数据
async function loadIndexBar() {
    const indices = [
        { id: 'idx-sh', code: 'sh000001' },
        { id: 'idx-sz', code: 'sz399001' },
        { id: 'idx-cy', code: 'sz399006' },
    ];
    
    for (const idx of indices) {
        const el = document.getElementById(idx.id);
        if (!el) continue;
        try {
            const resp = await fetch(`/api/stock/${idx.code}`);
            const data = await resp.text();
            const parsed = parseTencentData(data);
            if (parsed) {
                const changePct = parsed.changePercent;
                const isUp = changePct > 0;
                const changeClass = changePct > 0 ? 'up' : changePct < 0 ? 'down' : 'flat';
                const sign = changePct > 0 ? '+' : '';
                el.querySelector('.index-price').textContent = parsed.price;
                const changeEl = el.querySelector('.index-change');
                changeEl.textContent = `${sign}${changePct}%`;
                changeEl.className = 'index-change ' + changeClass;
            }
        } catch (e) { /* silent */ }
    }
}

function parseTencentData(data) {
    try {
        const lines = data.split(';');
        for (let line of lines) {
            if (line.startsWith('v_') && line.includes('=')) {
                const parts = line.split('=');
                const symbol = parts[0].substring(2);
                const content = parts[1].replace(/^"|"$/g, '');
                const fields = content.split('~');

                if (fields.length > 49) {
                    const price = parseFloat(fields[3]) || 0;
                    const yesterdayClose = parseFloat(fields[4]) || 0;
                    const changeVal = parseFloat(fields[31]) || (price - yesterdayClose).toFixed(2);
                    const changePct = parseFloat(fields[32]) || (yesterdayClose > 0 ? ((changeVal / yesterdayClose) * 100).toFixed(2) : '0');
                    const isUp = changeVal > 0;

                    const outerPlateRaw = parseInt(fields[7]) || 0;
                    const innerPlateRaw = parseInt(fields[8]) || 0;
                    const volumeRaw = parseInt(fields[36]) || 0;
                    const turnoverRaw = parseFloat(fields[37]) || 0;

                    const turnoverRate = parseFloat(fields[38]) || 0;
                    const pe = parseFloat(fields[39]) || 0;
                    const amplitude = parseFloat(fields[43]) || 0;
                    const volumeRatio = parseFloat(fields[49]) || 0;
                    const highLimit = parseFloat(fields[47]) || 0;
                    const lowLimit = parseFloat(fields[48]) || 0;
                    const circulateMarketCap = parseFloat(fields[44]) || 0;
                    const totalMarketCap = parseFloat(fields[45]) || 0;

                    const bid1Vol = parseInt(fields[12]) || 0;
                    const ask1Vol = parseInt(fields[22]) || 0;
                    const weibi = (bid1Vol + ask1Vol) > 0 ? (((bid1Vol - ask1Vol) / (bid1Vol + ask1Vol)) * 100).toFixed(2) : 0;

                    const avgPrice = volumeRaw > 0 ? ((turnoverRaw * 10000) / (volumeRaw * 100)).toFixed(2) : price.toFixed(2);
                    const avgPriceDeviation = parseFloat(avgPrice) > 0 ? (((price - parseFloat(avgPrice)) / parseFloat(avgPrice)) * 100).toFixed(2) : 0;

                    const plateDiff = outerPlateRaw - innerPlateRaw;
                    const outerRatio = (outerPlateRaw + innerPlateRaw) > 0 ? ((outerPlateRaw / (outerPlateRaw + innerPlateRaw)) * 100).toFixed(1) : '50';

                    return {
                        name: fields[1],
                        symbol: symbol,
                        price: fields[3],
                        priceRaw: price,
                        change: changeVal,
                        changePercent: changePct,
                        changeClass: isUp ? 'up' : 'down',
                        changeSymbol: (changeVal > 0 ? '+' : '') + changeVal.toFixed(2),
                        percentSymbol: (changePct > 0 ? '+' : '') + changePct.toFixed(2) + '%',
                        open: fields[5],
                        high: fields[33],
                        low: fields[34],
                        volume: fields[36],
                        volumeRaw: volumeRaw,
                        turnoverRaw: turnoverRaw,
                        turnover: turnoverRaw,
                        bid1: fields[11],
                        bid1Vol: fields[12],
                        ask1: fields[21],
                        ask1Vol: fields[22],
                        outerPlate: fields[7],
                        innerPlate: fields[8],
                        outerPlateRaw: outerPlateRaw,
                        innerPlateRaw: innerPlateRaw,
                        isUp: isUp,
                        turnoverRate: turnoverRate,
                        pe: pe,
                        amplitude: amplitude,
                        volumeRatio: volumeRatio,
                        highLimit: highLimit,
                        lowLimit: lowLimit,
                        circulateMarketCap: circulateMarketCap,
                        totalMarketCap: totalMarketCap,
                        weibi: parseFloat(weibi),
                        avgPrice: parseFloat(avgPrice),
                        avgPriceDeviation: parseFloat(avgPriceDeviation),
                        plateDiff: plateDiff,
                        outerRatio: parseFloat(outerRatio)
                    };
                }
            }
        }
    } catch (e) {
        console.error('解析腾讯数据失败:', e);
    }
    return null;
}

async function fetchStockQuote(symbol, silent = false) {
    const formattedSymbol = formatTencentSymbol(symbol);

    if (!/^(sh|sz)\d{6}$/i.test(formattedSymbol)) {
        if (!silent) {
            quoteContainer.innerHTML = `
                <div class="price-card" style="text-align:center;padding:2rem;">
                    <div style="font-size:1.2rem;color:#dc2626;margin-bottom:0.5rem;">⚠️ 请输入A股代码</div>
                    <div style="font-size:0.9rem;color:#64748b;line-height:1.8;">
                        支持<strong>所有沪深主板A股</strong>，直接输入6位代码即可<br>
                        例如：<code>603985</code>（恒润股份）、<code>000001</code>（平安银行）<br>
                        名称查询仅支持热门股票
                    </div>
                </div>
            `;
        }
        return;
    }
    
    try {
        const response = await fetch(`/api/stock/${formattedSymbol}`);
        if (!response.ok) throw new Error('服务器响应异常: ' + response.status);
        const data = await response.text();
        
        const parsedData = parseTencentData(data);
        
        if (parsedData) {
            renderQuoteCard(parsedData);
            updateLastUpdateTime();
        } else {
            throw new Error('数据解析失败，可能是非交易时段');
        }
    } catch (apiErr) {
        console.error('获取行情失败:', apiErr.message);
        
        if (!silent) {
            quoteContainer.innerHTML = `
                <div class="price-card" style="text-align:center;padding:2rem;">
                    <div style="font-size:1.2rem;color:#dc2626;margin-bottom:0.5rem;">⚠️ 获取数据失败</div>
                    <div style="font-size:0.9rem;color:#64748b;margin-bottom:1rem;">${escapeHtml(apiErr.message)}</div>
                    <button class="btn-primary" onclick="fetchStockQuote('${formattedSymbol}',false)" style="margin:0 auto;">🔄 重试</button>
                    <div style="font-size:0.75rem;color:#94a3b8;margin-top:0.8rem;">
                        非交易时段数据可能不完整，交易时段（9:30-15:00）数据最准确
                    </div>
                </div>
            `;
        }
    }
}


// 保存折叠状态
let savedCollapseStates = [];

function saveCollapseStates() {
    const sections = quoteContainer.querySelectorAll('.collapsible-section');
    savedCollapseStates = [];
    sections.forEach(section => {
        const title = section.querySelector('.collapsible-title');
        if (title) {
            savedCollapseStates.push({
                title: title.textContent,
                isCollapsed: section.classList.contains('collapsed')
            });
        }
    });
}

function restoreCollapseStates() {
    if (savedCollapseStates.length === 0) return;
    
    const sections = quoteContainer.querySelectorAll('.collapsible-section');
    sections.forEach(section => {
        const title = section.querySelector('.collapsible-title');
        if (title) {
            const savedState = savedCollapseStates.find(s => s.title === title.textContent);
            if (savedState) {
                if (savedState.isCollapsed) {
                    section.classList.add('collapsed');
                } else {
                    section.classList.remove('collapsed');
                }
            }
        }
    });
}

function renderQuoteCard(data) {
    // 保存当前折叠状态
    saveCollapseStates();

    const changeClass = data.isUp ? 'up' : 'down';
    const changePrefix = data.isUp ? '+' : '';

    const outerPlate = data.outerPlateRaw || 0;
    const innerPlate = data.innerPlateRaw || 0;
    const totalPlate = outerPlate + innerPlate;
    const outerRatio = data.outerRatio || (totalPlate > 0 ? ((outerPlate / totalPlate) * 100).toFixed(1) : '50');
    const plateDiff = (outerPlate - innerPlate).toLocaleString();
    const plateDiffClass = outerPlate > innerPlate ? 'buy' : 'sell';

    const price = data.priceRaw || parseFloat(data.price);
    const turnoverRate = data.turnoverRate || 0;
    const volumeRatio = data.volumeRatio || 0;
    const pe = data.pe || 0;
    const amplitude = data.amplitude || 0;
    const weibi = data.weibi || 0;
    const avgPrice = data.avgPrice || 0;
    const avgPriceDeviation = data.avgPriceDeviation || 0;
    const circulateMarketCap = data.circulateMarketCap || 0;

    const scoreResult = calculateScore(data);
    const analysis = getEnhancedAnalysis(data);

    const scoreBarColor = scoreResult.score >= 60 ? '#dc2626' : scoreResult.score >= 40 ? '#f59e0b' : '#16a34a';
    const scoreLabel = scoreResult.score >= 60 ? '看涨' : scoreResult.score >= 40 ? '观望' : '看跌';

    const displayOpen = parseFloat(data.open) > 0 ? data.open : '--';
    const displayHigh = parseFloat(data.high) > 0 ? data.high : '--';
    const displayLow = parseFloat(data.low) > 0 ? data.low : '--';

    quoteContainer.innerHTML = `
        <div class="price-card">
            <div class="stock-header">
                <div class="stock-name-code">
                    <div class="stock-name">${escapeHtml(data.name)}</div>
                    <div class="stock-code">${escapeHtml(data.symbol.toUpperCase())}</div>
                </div>
                <div class="stock-price-section">
                    <div class="stock-price ${changeClass}">${data.price}</div>
                    <div class="stock-change ${changeClass}">
                        ${changePrefix}${data.change} · ${data.changePercent}%
                    </div>
                </div>
            </div>

            <div style="display:flex;gap:0.6rem;margin-bottom:1rem;padding:0 0.25rem;">
                <button class="btn-secondary" style="flex:1;display:flex;align-items:center;justify-content:center;gap:0.4rem;background-color:#dbeafe;color:#1e40af;border-color:#bfdbfe;" onclick="openKlineModal('${data.symbol}')">
                    <span style="font-size:1.1rem;">📊</span> 查看日K线
                </button>
                <button class="btn-secondary" id="aiDiagnoseBtn" style="flex:1;display:flex;align-items:center;justify-content:center;gap:0.4rem;background-color:#fef3c7;color:#92400e;border-color:#fde68a;" onclick="startAiDiagnose('${data.symbol}')">
                    <span style="font-size:1.1rem;">🤖</span> AI诊断
                </button>
            </div>
            <div class="stock-card-actions" style="padding:0 0.25rem;">
                <button class="add-to-hot-btn" id="addToHotBtn" onclick="handleAddToHotStocks('${data.symbol}', '${escapeHtml(data.name)}')">
                    ⭐ 添加到热门股票
                </button>
            </div>

            <!-- 可折叠的股票详细数据 -->
            <div class="collapsible-section collapsed">
                <div class="collapsible-header" onclick="toggleCollapse(this)">
                    <span class="collapsible-title">📋 详细数据</span>
                    <span class="collapsible-arrow">▼</span>
                </div>
                <div class="collapsible-content" id="stockGridContent">
                    <div class="stock-grid">
                        <div class="grid-item">
                            <div class="grid-label">今开</div>
                            <div class="grid-value">${displayOpen}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">最高</div>
                            <div class="grid-value">${displayHigh}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">最低</div>
                            <div class="grid-value">${displayLow}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">成交量</div>
                            <div class="grid-value">${formatNumber(data.volumeRaw || data.volume)}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">成交额</div>
                            <div class="grid-value">${formatNumber(data.turnover * 10000)}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">换手率</div>
                            <div class="grid-value">${turnoverRate.toFixed(2)}%</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">量比</div>
                            <div class="grid-value" style="color:${volumeRatio > 1.5 ? '#dc2626' : volumeRatio < 0.5 ? '#16a34a' : '#1e293b'}">${volumeRatio.toFixed(2)}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">市盈率</div>
                            <div class="grid-value">${pe > 0 ? pe.toFixed(2) : '亏损'}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">振幅</div>
                            <div class="grid-value">${amplitude.toFixed(2)}%</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">委比</div>
                            <div class="grid-value" style="color:${weibi > 0 ? '#dc2626' : '#16a34a'}">${weibi > 0 ? '+' : ''}${weibi.toFixed(2)}%</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">均价</div>
                            <div class="grid-value">${avgPrice.toFixed(2)}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">买一</div>
                            <div class="grid-value">${data.bid1}</div>
                        </div>
                        <div class="grid-item">
                            <div class="grid-label">卖一</div>
                            <div class="grid-value">${data.ask1}</div>
                        </div>
                        ${circulateMarketCap > 0 ? `
                        <div class="grid-item">
                            <div class="grid-label">流通市值</div>
                            <div class="grid-value">${formatNumber(circulateMarketCap * 10000)}</div>
                        </div>
                        ` : ''}
                    </div>
                </div>
            </div>
        </div>

        <!-- 可折叠的综合评分条 -->
        <div class="collapsible-section">
            <div class="collapsible-header" onclick="toggleCollapse(this)">
                <span class="collapsible-title">📊 综合评分模型</span>
                <span class="collapsible-arrow">▼</span>
            </div>
            <div class="collapsible-content">
                <div class="score-card" style="margin-bottom:0;border-left:none;">
                    <div class="score-header">
                        <span class="score-title"></span>
                        <span class="score-badge" style="background:${scoreBarColor}">${scoreLabel}</span>
                    </div>
                    <div class="score-bar-track">
                        <div class="score-bar-fill" style="width:${scoreResult.score}%;background:${scoreBarColor}"></div>
                    </div>
                    <div class="score-value">${scoreResult.score}分 · ${scoreResult.label}</div>
                    <div class="score-details">
                        ${scoreResult.details.map(d => `
                        <div class="score-detail-item">
                            <span>${d.label}</span>
                            <span style="color:${d.score >= d.max * 0.6 ? '#dc2626' : d.score >= d.max * 0.4 ? '#f59e0b' : '#16a34a'}">${d.score}/${d.max}</span>
                        </div>
                        `).join('')}
                    </div>
                </div>
            </div>
        </div>

        ${outerPlate > 0 || innerPlate > 0 ? `
        <!-- 可折叠的内外盘数据 -->
        <div class="collapsible-section">
            <div class="collapsible-header" onclick="toggleCollapse(this)">
                <span class="collapsible-title">📊 内外盘数据</span>
                <span class="collapsible-arrow">▼</span>
            </div>
            <div class="collapsible-content">
                <div class="orderbook-card" style="margin-bottom:0;border-left:none;">
                    <div class="orderbook-grid">
                        <div class="orderbook-side buy">
                            <div class="side-label buy">外盘 · 主动买入</div>
                            <div class="side-value buy">${formatNumber(outerPlate)}</div>
                            <div class="side-sub">看多力量</div>
                        </div>
                        <div class="orderbook-side sell">
                            <div class="side-label sell">内盘 · 主动卖出</div>
                            <div class="side-value sell">${formatNumber(innerPlate)}</div>
                            <div class="side-sub">看空力量</div>
                        </div>
                    </div>
                    <div class="orderbook-stats">
                        <span>差值：<strong class="${plateDiffClass}">${plateDiff}</strong></span>
                        <span>外盘占比：<strong class="buy">${outerRatio}%</strong></span>
                    </div>
                </div>
            </div>
        </div>
        ` : ''}

        ${analysis && !analysis.disabled ? `
        <!-- 可折叠的分析卡片 -->
        <div class="collapsible-section">
            <div class="collapsible-header" onclick="toggleCollapse(this)">
                <span class="collapsible-title">${analysis.title}</span>
                <span class="collapsible-arrow">▼</span>
            </div>
            <div class="collapsible-content">
                <div class="analysis-card ${analysis.type}" style="margin-bottom:0;">
                    <div class="analysis-desc">
                        <div style="margin-bottom:6px;font-weight:700;">${analysis.desc}</div>
                        ${analysis.reason ? `<div style="font-size:0.75rem;color:#6b7280;margin-bottom:4px;">📌 ${analysis.reason}</div>` : ''}
                        ${analysis.position ? `<div style="font-size:0.75rem;color:#6b7280;margin-bottom:4px;">📍 位置：${analysis.position}</div>` : ''}
                        ${analysis.volumeDesc ? `<div style="font-size:0.75rem;color:#6b7280;margin-bottom:4px;">📊 量能：${analysis.volumeDesc}</div>` : ''}
                        <div style="font-size:0.8rem;color:#475569;margin-top:8px;padding:8px 10px;background:rgba(255,255,255,0.6);border-radius:8px;">👉 ${analysis.action}</div>
                    </div>
                </div>
            </div>
        </div>
        ` : ''}
        ${analysis && analysis.disabled ? `
        <div class="collapsible-section">
            <div class="collapsible-header" onclick="toggleCollapse(this)">
                <span class="collapsible-title">⛔ 策略不可用</span>
                <span class="collapsible-arrow">▼</span>
            </div>
            <div class="collapsible-content">
                <div class="analysis-card hold" style="margin-bottom:0;">
                    <div class="analysis-desc">${analysis.reason}</div>
                </div>
            </div>
        </div>
        ` : ''}
    `;

    // 恢复折叠状态
    restoreCollapseStates();
    
    // 更新添加到热门按钮状态
    updateAddToHotBtn(data.symbol);
}

function calculateScore(data) {
    const price = data.priceRaw || parseFloat(data.price);
    const outerRatio = data.outerRatio || 50;
    const turnoverRate = data.turnoverRate || 0;
    const volumeRatio = data.volumeRatio || 0;
    const weibi = data.weibi || 0;
    const avgPriceDeviation = data.avgPriceDeviation || 0;
    const amplitude = data.amplitude || 0;
    const changePercent = data.changePercent || 0;
    const pe = data.pe || 0;

    const details = [];

    // 1. 外盘占比 (权重 20)
    let scoreOuter = 5;
    if (outerRatio >= 60) scoreOuter = 20;
    else if (outerRatio >= 55) scoreOuter = 15;
    else if (outerRatio >= 50) scoreOuter = 10;
    details.push({ label: '外盘占比(' + outerRatio.toFixed(1) + '%)', score: scoreOuter, max: 20 });

    // 2. 量比健康度 (权重 15)
    let scoreVolume = 5;
    if (volumeRatio > 1.8 && outerRatio < 52) scoreVolume = 0; // 放量出货检测
    else if (volumeRatio >= 1.5) scoreVolume = outerRatio > 55 ? 10 : 5;
    else if (volumeRatio >= 1) scoreVolume = 15;
    else if (volumeRatio >= 0.5) scoreVolume = 8;
    details.push({ label: '量比(' + volumeRatio.toFixed(2) + ')', score: scoreVolume, max: 15 });

    // 3. 换手率 (权重 10)
    let scoreTurnover = 3;
    if (turnoverRate >= 3 && turnoverRate <= 10) scoreTurnover = 10;
    else if (turnoverRate >= 1 && turnoverRate < 3) scoreTurnover = 7;
    else if (turnoverRate > 10) scoreTurnover = 5;
    details.push({ label: '换手率(' + turnoverRate.toFixed(2) + '%)', score: scoreTurnover, max: 10 });

    // 4. 涨幅及强势股逻辑 (权重 20)
    let scoreChange = 0;
    if (changePercent > 9.5) {
        scoreChange = 5; // 接近涨停，避险为主
    } else if (changePercent > 7) {
        scoreChange = 12; // 极高位强势
    } else if (changePercent > 3) {
        // 强势区间：量价齐升则给满分，否则给高分
        scoreChange = (volumeRatio >= 1.2 && outerRatio > 58) ? 20 : 15;
    } else if (changePercent > 0) {
        scoreChange = 20; // 稳健启动
    } else if (changePercent > -2) {
        scoreChange = 10;
    }
    details.push({ label: '涨幅(' + (changePercent > 0 ? '+' : '') + changePercent.toFixed(2) + '%)', score: scoreChange, max: 20 });

    // 5. 委比真实性 (权重 10)
    let scoreWeibi = 2;
    if (weibi >= 99 && outerRatio < 60) scoreWeibi = 0; // 虚假封单过滤
    else if (weibi > 30) scoreWeibi = 10;
    else if (weibi > 0) scoreWeibi = 7;
    else if (weibi > -30) scoreWeibi = 4;
    details.push({ label: '委比(' + (weibi > 0 ? '+' : '') + weibi.toFixed(2) + '%)', score: scoreWeibi, max: 10 });

    // 6. 均价偏离度 (权重 15)
    let scoreAvg = 2;
    if (avgPriceDeviation > 1.5) scoreAvg = 5;
    else if (avgPriceDeviation > 0) scoreAvg = 15;
    else if (avgPriceDeviation > -1) scoreAvg = 8;
    details.push({ label: '均价偏离(' + (avgPriceDeviation > 0 ? '+' : '') + avgPriceDeviation.toFixed(2) + '%)', score: scoreAvg, max: 15 });

    // 7. 振幅 (权重 5)
    let scoreAmplitude = 2;
    if (amplitude > 2 && amplitude <= 6) scoreAmplitude = 5;
    else if (amplitude > 1 && amplitude <= 2) scoreAmplitude = 3;
    details.push({ label: '振幅(' + amplitude.toFixed(2) + '%)', score: scoreAmplitude, max: 5 });

    // 8. 估值/位置修正 (权重 5)
    let scorePosition = 5;
    if (pe < 0) scorePosition = 2;
    details.push({ label: '基本面修正', score: scorePosition, max: 5 });

    const totalScore = scoreOuter + scoreVolume + scoreTurnover + scoreChange + scoreWeibi + scoreAvg + scoreAmplitude + scorePosition;
    const normalized = Math.min(100, Math.round(totalScore));

    let label = '';
    if (normalized >= 80) label = '强烈看涨';
    else if (normalized >= 70) label = '建议关注';
    else if (normalized >= 55) label = '中性观望';
    else if (normalized >= 40) label = '谨慎回调';
    else label = '强烈避险';

    return { score: normalized, label: label, details: details };
}

// ============ 策略核心函数 ============

function getPlateRelation(data) {
    const outer = data.outerPlateRaw || 0;
    const inner = data.innerPlateRaw || 0;
    if (outer === 0 && inner === 0) return 'none';
    if (outer === 0) return 'innerOnly';
    if (inner === 0) return 'outerOnly';

    const ratio = inner / outer;
    if (ratio >= 1.3) return 'innerStrong';
    if (ratio <= 1 / 1.3) return 'outerStrong';
    if (ratio >= 0.9 && ratio <= 1.1) return 'balanced';
    if (ratio > 1.1) return 'slightInner';
    return 'slightOuter';
}

function getVolumeLevel(data) {
    const vr = data.volumeRatio || 0;
    if (vr >= 1.5) return 'expansion';
    if (vr < 0.5) return 'shrinkage';
    if (vr >= 1) return 'slightExpand';
    return 'slightShrink';
}

function getPositionLevel(data) {
    const cp = Math.abs(data.changePercent || 0);
    const pe = data.pe || 0;
    if (cp > 8 || (pe > 0 && pe > 500)) return '高位';
    if (cp > 3 || (pe > 0 && pe > 200)) return '中位';
    if (cp < 1 && pe > 0 && pe < 50) return '低位';
    return '中位';
}

function getPriceVsAvgLine(data) {
    const price = data.priceRaw || parseFloat(data.price);
    const avg = data.avgPrice || 0;
    if (avg <= 0) return 'unknown';
    return price >= avg ? 'above' : 'below';
}

function isDisabledScenario(data) {
    const price = data.priceRaw || parseFloat(data.price);
    const highLimit = data.highLimit || 0;
    const lowLimit = data.lowLimit || 0;
    const volume = data.volumeRaw || 0;
    const outerPlate = data.outerPlateRaw || 0;
    const innerPlate = data.innerPlateRaw || 0;

    if (highLimit > 0 && price >= highLimit * 0.999) {
        return { reason: '涨停板附近，内外盘失真失效' };
    }
    if (lowLimit > 0 && price <= lowLimit * 1.001) {
        return { reason: '跌停板附近，内外盘失真失效' };
    }
    if (volume === 0 || (outerPlate === 0 && innerPlate === 0)) {
        return { reason: '无有效成交数据，策略不可用' };
    }
    return null;
}

function getEnhancedAnalysis(data) {
    const disabled = isDisabledScenario(data);
    if (disabled) return { disabled: true, reason: disabled.reason };

    const plateRel = getPlateRelation(data);
    const volLevel = getVolumeLevel(data);
    const position = getPositionLevel(data);
    const priceVsAvg = getPriceVsAvgLine(data);
    const change = data.change || 0;
    const dayUp = change > 0;
    const dayDown = change < 0;
    const changePercent = data.changePercent || 0;
    const volumeRatio = data.volumeRatio || 0;

    const volLabel = volLevel === 'expansion' ? '放量' : volLevel === 'shrinkage' ? '缩量' : '平量';
    const volDesc = `量比${volumeRatio.toFixed(2)}·${volLabel}`;

    // 模型1：内盘强于外盘 + 股价上涨 → 主力暗吸筹
    if ((plateRel === 'innerStrong' || plateRel === 'slightInner') && dayUp) {
        if (position === '高位') {
            return {
                type: 'bearish', title: '⚠️ 高位疑为诱多',
                desc: '内盘>外盘但股价上涨，位置在高位',
                reason: '高位吸筹不成立，极大可能是主力诱多派发',
                position: position, volumeDesc: volDesc,
                action: '不建议参与，已有仓位减仓止盈'
            };
        }
        const isShrinkOrNormal = volLevel === 'shrinkage' || volLevel === 'slightShrink';
        if (priceVsAvg === 'above' && isShrinkOrNormal) {
            return {
                type: 'bullish', title: '📈 模型1：低位暗吸筹（最优）',
                desc: '内盘>外盘·缩量/平量上涨·分时站均线上',
                reason: '主动卖单多但股价抗跌上涨，分时均价线上方·缩量无出货·拆单特征不明显',
                position: position, volumeDesc: volDesc,
                action: '持股锁仓不动，不做T不轻易下车；低位可分批加仓'
            };
        }
        return {
            type: 'bullish', title: '📈 模型1：主力暗吸筹',
            desc: '内盘>外盘但股价上涨',
            reason: '主动卖单多但股价反而上涨，主力小单砸、托单低吸',
            position: position, volumeDesc: volDesc,
            action: '持股不动，观察量能和分时均价变化'
        };
    }

    // 模型2：内盘强 + 放量下跌 → 坚决出逃
    if ((plateRel === 'innerStrong' || plateRel === 'slightInner') && dayDown) {
        if (volLevel !== 'expansion' && volLevel !== 'slightExpand') {
            return {
                type: 'bearish', title: '📉 模型2：内盘大但缩量阴跌',
                desc: '内盘>外盘下跌但未放量',
                reason: '阴跌缩量不算主卖出逃，可能是弱势盘整',
                position: position, volumeDesc: volDesc,
                action: '观望为主，若持续缩量下行则减仓'
            };
        }
        if (priceVsAvg === 'below') {
            let strength = position === '高位' ? '（最强离场信号）' : '';
            return {
                type: 'bearish', title: '📉 模型2：主力坚决出逃' + strength,
                desc: '内盘>外盘·放量下跌·分时均线下',
                reason: '主动卖单蜂拥砸盘，分时始终在均价线下方，反抽无力，放量确认',
                position: position, volumeDesc: volDesc,
                action: '不分盈亏果断离场，不抄底不补仓！全位置通用，高位最强'
            };
        }
        return {
            type: 'bearish', title: '📉 模型2：主力出逃信号',
            desc: '内盘>外盘·放量下跌',
            reason: '主动卖单放量砸盘，资金集体跑路',
            position: position, volumeDesc: volDesc,
            action: '考虑减仓或离场，观察分时均价线'
        };
    }

    // 模型3：外盘强 + 股价下跌 → 诱多出货
    if ((plateRel === 'outerStrong' || plateRel === 'slightOuter') && dayDown) {
        if (priceVsAvg === 'below') {
            return {
                type: 'bearish', title: '📉 模型3：典型诱多出货',
                desc: '外盘>内盘·股价下跌·分时均线下',
                reason: '外盘大但股价站不上昨收、分时均价线下方，大买单托盘制造抢筹假象，真实卖单拆小单悄悄砸',
                position: position, volumeDesc: volDesc,
                action: '坚决不抄底不接盘！任何位置都只卖不买，高位中位尤其危险'
            };
        }
        return {
            type: 'bearish', title: '📉 模型3：疑似诱多出货',
            desc: '外盘>内盘但股价下跌',
            reason: '主动买单多但价格一直在昨收下方震荡，需留意大买单托盘、小卖单出货的拆单手法',
            position: position, volumeDesc: volDesc,
            action: '不抄底不追入，观察分时均价线确认方向'
        };
    }

    // 模型4：外盘强 + 股价上涨 → 量价齐升
    if ((plateRel === 'outerStrong' || plateRel === 'slightOuter') && dayUp) {
        if (position === '高位' && volLevel === 'expansion') {
            return {
                type: 'watch', title: '⚡ 模型4变种：高位放量追涨',
                desc: '外盘>内盘·涨·高位爆量',
                reason: '高位急涨+爆量要防次日分歧和最后一波诱多',
                position: position, volumeDesc: volDesc,
                action: '高位只止盈不追高，已有仓位可分批卖出锁定利润'
            };
        }
        if (priceVsAvg === 'above') {
            return {
                type: 'bullish', title: '📈 模型4：量价齐升多头强势',
                desc: '外盘>内盘·上涨·分时均线上',
                reason: '主动买入旺盛配合成交量，股价稳步站上昨收+分时均价线上方，真实抢筹',
                position: position, volumeDesc: volDesc,
                action: position === '高位' ? '高位只止盈不追高' : '中低位持股不动，可小仓位加仓'
            };
        }
        return {
            type: 'bullish', title: '📈 模型4：多头看涨',
            desc: '外盘>内盘且今日上涨',
            reason: '主动买单多+股价涨，多头占优',
            position: position, volumeDesc: volDesc,
            action: '持有观察，注意分时均价线方向'
        };
    }

    // 模型5：内外盘小而缩量 → 锁仓
    if (volLevel === 'shrinkage' && data.outerPlateRaw + data.innerPlateRaw < 5000000) {
        if (Math.abs(changePercent) < 1.5) {
            return {
                type: 'hold', title: '🔒 模型5：筹码锁仓洗盘',
                desc: '内外盘极小·缩量明显·窄幅波动',
                reason: '主动买卖都极少，没人砸也没人抢，主力锁仓洗盘等风来',
                position: position, volumeDesc: volDesc,
                action: '不交易不折腾，耐心等突破信号，不用频繁做T；底部横盘最佳'
            };
        }
        return {
            type: 'hold', title: '🔒 模型5：筹码锁仓',
            desc: '成交缩量低迷',
            reason: '主动买卖盘极小，主力锁仓中',
            position: position, volumeDesc: volDesc,
            action: '耐心持有，等待放量选方向'
        };
    }

    // 模型6：内外盘均衡 + 放量 → 即将变盘
    if ((plateRel === 'balanced') && volLevel === 'expansion') {
        return {
            type: 'watch', title: '⚡ 模型6：多空博弈即将变盘',
            desc: '内外盘均衡·明显放量',
            reason: '主动买卖力量相当但突然放量，箱体末端或关键支撑/压力位选择方向',
            position: position, volumeDesc: volDesc,
            action: '纯观望不提前布局！等向上突破再进、向下跌破再离场，不做预判'
        };
    }

    // 默认兜底
    const trend = dayUp ? '偏多' : dayDown ? '偏空' : '平盘';
    return {
        type: 'hold', title: '🔍 综合信号不明确',
        desc: '当前盘面' + trend + '·' + position + '·' + volDesc,
        reason: '内外盘未见显著强弱差距，量能方向不明确，建议观望',
        position: position, volumeDesc: volDesc,
        action: '轻仓观望，等待明确信号出现再操作'
    };
}

function renderInfoMsg(msg) {
    console.log(msg);
}

function toggleCollapse(header) {
    const section = header.closest('.collapsible-section');
    if (section) {
        section.classList.toggle('collapsed');
    }
}

function toggleHint() {
    const hint = document.querySelector('.collapsible-hint');
    if (hint) {
        hint.classList.toggle('collapsed');
    }
}

function escapeHtml(str) {
    if (!str) return '';
    return str.replace(/[<>&]/g, function(m) {
        if (m === '&') return '&';
        if (m === '<') return '<';
        if (m === '>') return '>';
        return m;
    });
}

async function onFetchOrRefresh() {
    let input = stockInput.value.trim();
    if (input === '') {
        showNotification('请输入股票代码或名称');
        return;
    }
    
    let querySymbol = input.toLowerCase();
    
    // 支持按名称查询（支持模糊匹配）
    const nameMap = getStockNameMap();
    if (nameMap[input]) {
        querySymbol = nameMap[input];
    } else {
        // 尝试模糊匹配
        const matchedSymbols = [];
        const stockCodeMap = getStockCodeMap();
        for (const [symbol, name] of Object.entries(stockCodeMap)) {
            if (name && name.includes(input)) {
                matchedSymbols.push({ symbol, name });
            }
        }
        
        if (matchedSymbols.length === 1) {
            querySymbol = matchedSymbols[0].symbol;
        } else if (matchedSymbols.length > 1) {
            // 显示搜索结果选择
            showSearchResults(matchedSymbols);
            return;
        } else if (!/^(sh|sz)\d{6}$/.test(querySymbol)) {
            // 尝试补全纯数字代码
            if (/^\d{6}$/.test(querySymbol)) {
                if (querySymbol.startsWith('6')) querySymbol = 'sh' + querySymbol;
                else if (querySymbol.startsWith('0') || querySymbol.startsWith('3') || querySymbol.startsWith('00') || querySymbol.startsWith('30')) querySymbol = 'sz' + querySymbol;
            }
        }
    }
    
    currentSymbol = querySymbol;
    await fetchStockQuote(querySymbol, false);
}

// 显示搜索结果
function showSearchResults(matches) {
    let html = '<div class="search-results-modal">';
    html += '<div class="search-results-header">找到 ' + matches.length + ' 只股票，请选择：</div>';
    html += '<div class="search-results-list">';
    
    matches.forEach(match => {
        html += '<div class="search-result-item" onclick="selectSearchResult(\'' + match.symbol + '\')">';
        html += '<span class="stock-name">' + escapeHtml(match.name) + '</span>';
        html += '<span class="stock-code">' + match.symbol.toUpperCase() + '</span>';
        html += '</div>';
    });
    
    html += '</div>';
    html += '<div class="search-results-close" onclick="closeSearchResults()">关闭</div>';
    html += '</div>';
    
    let modal = document.getElementById('search-results-modal');
    if (!modal) {
        modal = document.createElement('div');
        modal.id = 'search-results-modal';
        document.body.appendChild(modal);
    }
    modal.innerHTML = html;
    modal.style.display = 'block';
}

// 选择搜索结果
function selectSearchResult(symbol) {
    closeSearchResults();
    currentSymbol = symbol.toLowerCase();
    stockInput.value = currentSymbol;
    fetchStockQuote(currentSymbol, false);
}

// 关闭搜索结果
function closeSearchResults() {
    const modal = document.getElementById('search-results-modal');
    if (modal) {
        modal.style.display = 'none';
    }
}

async function manualRefresh() {
    if (currentSymbol) {
        await fetchStockQuote(currentSymbol, false);
    }
}

// 智能刷新调度函数
function scheduleRefresh() {
    stopAutoRefresh();
    if (!currentSymbol) return;
    
    const refreshOnce = async () => {
        try {
            if (currentSymbol) {
                await fetchStockQuote(currentSymbol, true);
            }
        } catch (e) {
            console.error('自动刷新失败:', e);
        }
        
        // 计算下一次刷新时间
        const nextDelay = getNextRefreshDelay();
        autoRefreshInterval = setTimeout(refreshOnce, nextDelay);
    };
    
    // 立即开始第一次刷新
    refreshOnce();
}

function stopAutoRefresh() {
    if (autoRefreshInterval) {
        clearTimeout(autoRefreshInterval);
        autoRefreshInterval = null;
    }
}

// 获取交易状态显示文字
function getTradingStatusText() {
    const now = new Date();
    if (isTradingTime()) {
        return '📈 交易时间，自动1秒刷新';
    }
    
    const day = now.getDay();
    const hours = now.getHours();
    const minutes = now.getMinutes();
    
    if (day === 0 || day === 6) {
        return '📅 周末休市中';
    }
    
    if (hours < 9 || (hours === 9 && minutes < 30)) {
        return '⏰ 等待上午9:30开盘';
    }
    
    if (hours >= 11 && (hours < 13 || (hours === 12 && minutes < 30))) {
        return '☕ 午休中，等待下午1:00';
    }
    
    if (hours >= 15) {
        return '🌙 今日交易已结束';
    }
    
    return '💤 非交易时间';
}

let scanAbortController = null;
let isScanning = false;

function getAllStockCodes() {
    // 使用配置文件中的扫描股票池
    const pool = getScanStockPool();
    if (pool && pool.length > 0) {
        return pool;
    }
    // 备用方案：从名称映射中提取
    const seen = new Set();
    const nameMap = getStockNameMap();
    for (const name in nameMap) {
        const code = nameMap[name];
        if (!seen.has(code) && /^(sh|sz)\d{6}$/i.test(code) && !code.startsWith('sh000') && !code.startsWith('sz399')) {
            seen.add(code);
        }
    }
    return Array.from(seen);
}

async function startScan(silent = false) {
    if (isScanning) return;

    const scanProgress = document.getElementById('scanProgress');
    const scanSummary = document.getElementById('scanSummary');
    const scanResults = document.getElementById('scanResults');

    isScanning = true;
    scanAbortController = new AbortController();

    if (!silent) {
        scanProgress.style.display = 'flex';
        scanSummary.style.display = 'none';
        scanResults.innerHTML = '';
    }

    const allCodes = getAllStockCodes();
    if (!silent) {
        scanProgress.innerHTML = `<div class="loading-spinner" style="width:20px;height:20px;border-width:2px;margin:0;"></div> 正在扫描 ${allCodes.length} 只股票...`;
    }

    try {
        const response = await fetch(`/api/hot-stocks-scan?codes=${allCodes.join(',')}`, {
            signal: scanAbortController.signal
        });

        if (!response.ok) throw new Error('扫描请求失败');

        const results = await response.json();

        if (results.error) {
            if (!silent) scanProgress.innerHTML = '❌ ' + results.error;
            resetScanUI();
            return;
        }

        if (scanAbortController.signal.aborted) {
            if (!silent) scanProgress.innerHTML = '扫描已取消';
            resetScanUI();
            return;
        }

        if (results.length === 0) {
            if (!silent) scanProgress.innerHTML = '⚠️ 未找到有效数据，请稍后重试（非交易时段可能无数据）';
            resetScanUI();
            return;
        }

        const bullCount = results.filter(r => r.score >= 60).length;
        const bearCount = results.filter(r => r.score < 40).length;
        const neutralCount = results.length - bullCount - bearCount;

        scanSummary.style.display = 'flex';
        scanSummary.innerHTML = `
            <div class="scan-stat bull">📈 看涨: ${bullCount}只</div>
            <div class="scan-stat bear">📉 看跌: ${bearCount}只</div>
            <div class="scan-stat neutral">⏸ 观望: ${neutralCount}只</div>
            <div class="scan-stat neutral">📊 共扫描: ${results.length}只</div>
        `;

        renderScanResults(results);
        if (!silent) {
            scanProgress.innerHTML = `✅ 扫描完成！共 ${results.length} 只股票`;
        }
    } catch (err) {
        if (err.name === 'AbortError') {
            if (!silent) scanProgress.innerHTML = '扫描已取消';
        } else {
            console.error('扫描失败:', err);
            if (!silent) scanProgress.innerHTML = '❌ 扫描失败: ' + err.message;
        }
    } finally {
        resetScanUI();
    }
}

function stopScan() {
    if (scanAbortController) {
        scanAbortController.abort();
        isScanning = false;
    }
    resetScanUI();
}

function resetScanUI() {
    isScanning = false;
}

function updateScanResults(results) {
    const scanResults = document.getElementById('scanResults');
    if (results.length === 0) return;
    hasScanResult = true;
    updateScanLastUpdateTime();

    const newMap = {};
    results.forEach(r => { newMap[r.symbol] = r; });

    const table = scanResults.querySelector('.stock-table');
    if (!table) { renderScanResults(results); return; }

    const existingRows = scanResults.querySelectorAll('.stock-table-row');
    const rowMap = {};
    const existingSymbols = new Set();

    existingRows.forEach(row => {
        const sym = row.getAttribute('data-symbol');
        existingSymbols.add(sym);
        rowMap[sym] = row;
        const r = newMap[sym];
        if (!r) { row.style.opacity = '0.3'; return; }
        row.style.opacity = '1';

        const changeClass = r.change >= 0 ? 'color:#dc2626;' : 'color:#16a34a;';
        const changePrefix = r.change >= 0 ? '+' : '';
        let tagClass = 'tag-hold'; let tagText = '观望';
        if (r.score >= 60) { tagClass = 'tag-bull'; tagText = '看涨'; }
        else if (r.score < 40) { tagClass = 'tag-bear'; tagText = '看跌'; }

        const priceEl = row.querySelector('.price');
        const changeEl = row.querySelector('.change-pct');
        const volumeRatioEl = row.querySelector('.volume-ratio');
        const turnoverEl = row.querySelector('.turnover');
        const weibiEl = row.querySelector('.weibi-val');
        const scoreEl = row.querySelector('.score-cell');

        if (priceEl) { priceEl.style.cssText = changeClass; priceEl.textContent = r.price; }
        if (changeEl) { changeEl.style.cssText = changeClass + 'font-weight:600;'; changeEl.textContent = changePrefix + r.changePercent.toFixed(2) + '%'; }
        if (volumeRatioEl) volumeRatioEl.textContent = r.volumeRatio.toFixed(2);
        if (turnoverEl) turnoverEl.textContent = r.turnoverRate.toFixed(2) + '%';
        if (weibiEl) {
            weibiEl.style.cssText = 'color:' + (r.weibi >= 0 ? '#dc2626' : '#16a34a') + ';font-weight:600;';
            weibiEl.textContent = (r.weibi >= 0 ? '+' : '') + r.weibi.toFixed(2) + '%';
        }
        if (scoreEl) {
            scoreEl.style.color = r.score >= 60 ? '#dc2626' : r.score >= 40 ? '#f59e0b' : '#16a34a';
            scoreEl.innerHTML = r.score + '<br><span class="' + tagClass + '">' + tagText + '</span>';
        }
        row.classList.add('flash-update');
        setTimeout(() => row.classList.remove('flash-update'), 600);
    });

    // 按评分降序重排行顺序
    const sortedResults = [...results].sort((a, b) => b.score - a.score);
    sortedResults.forEach(r => {
        const row = rowMap[r.symbol];
        if (row && table) {
            table.appendChild(row);
        }
    });

    // 新增股票追加到末尾
    results.forEach(r => {
        if (existingSymbols.has(r.symbol)) return;
        const changeClass = r.change >= 0 ? 'color:#dc2626;' : 'color:#16a34a;';
        const changePrefix = r.change >= 0 ? '+' : '';
        const stockTagsHtml = renderScanTags(r.symbol);
        let tagClass = 'tag-hold'; let tagText = '观望';
        if (r.score >= 60) { tagClass = 'tag-bull'; tagText = '看涨'; }
        else if (r.score < 40) { tagClass = 'tag-bear'; tagText = '看跌'; }
        const row = document.createElement('div');
        row.className = 'stock-table-row';
        row.setAttribute('data-symbol', r.symbol);
        row.innerHTML = `<span class="name">${escapeHtml(r.name)}<br><small style="color:#94a3b8;font-weight:400;">${r.symbol.toUpperCase()}</small>${stockTagsHtml}</span><span class="price" style="${changeClass}">${r.price}</span><span class="change-pct" style="${changeClass}font-weight:600;">${changePrefix}${r.changePercent.toFixed(2)}%</span><span class="volume-ratio">${r.volumeRatio.toFixed(2)}</span><span class="turnover">${r.turnoverRate.toFixed(2)}%</span><span class="weibi-val" style="color:${r.weibi >= 0 ? '#dc2626' : '#16a34a'};font-weight:600;">${r.weibi >= 0 ? '+' : ''}${r.weibi.toFixed(2)}%</span><span class="score-cell" style="color:${r.score >= 60 ? '#dc2626' : r.score >= 40 ? '#f59e0b' : '#16a34a'}">${r.score}<br><span class="${tagClass}">${tagText}</span></span>`;
        row.setAttribute('onclick', "document.getElementById('tab-quote').classList.add('active');document.getElementById('tab-scan').classList.remove('active');document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));document.querySelector('[data-tab=tab-quote]').classList.add('active');stockInput.value='" + r.symbol + "';currentSymbol='" + r.symbol + "';fetchStockQuote('" + r.symbol + "',false);");
        if (table) table.appendChild(row);
    });
}

function renderScanResults(results) {
    const scanResults = document.getElementById('scanResults');
    if (results.length === 0) {
        scanResults.innerHTML = '<div style="text-align:center;padding:2rem;color:#64748b;">暂无扫描结果</div>';
        return;
    }

    hasScanResult = true;
    updateScanLastUpdateTime();

    // 按评分降序排列
    const sortedResults = [...results].sort((a, b) => b.score - a.score);

    let html = '<div class="stock-table"><div class="stock-table-header">';
    html += '<span>名称</span><span>现价</span><span>涨跌幅</span><span>量比</span><span>换手率</span><span>委比</span><span>综合评分</span>';
    html += '</div>';

    sortedResults.forEach(r => {
        const changeClass = r.change >= 0 ? 'color:#dc2626;' : 'color:#16a34a;';
        const changePrefix = r.change >= 0 ? '+' : '';
        let tagClass = 'tag-hold';
        let tagText = '观望';
        if (r.score >= 60) { tagClass = 'tag-bull'; tagText = '看涨'; }
        else if (r.score < 40) { tagClass = 'tag-bear'; tagText = '看跌'; }
        const stockTagsHtml = renderScanTags(r.symbol);

        html += `
            <div class="stock-table-row" data-symbol="${r.symbol}" onclick="document.getElementById('tab-quote').classList.add('active');document.getElementById('tab-scan').classList.remove('active');document.querySelectorAll('.tab-btn').forEach(b=>b.classList.remove('active'));document.querySelector('[data-tab=tab-quote]').classList.add('active');stockInput.value='${r.symbol}';currentSymbol='${r.symbol}';fetchStockQuote('${r.symbol}',false);">
                <span class="name">${escapeHtml(r.name)}<br><small style="color:#94a3b8;font-weight:400;">${r.symbol.toUpperCase()}</small>${stockTagsHtml}</span>
                <span class="price" style="${changeClass}">${r.price}</span>
                <span class="change-pct" style="${changeClass}font-weight:600;">${changePrefix}${r.changePercent.toFixed(2)}%</span>
                <span class="volume-ratio">${r.volumeRatio.toFixed(2)}</span>
                <span class="turnover">${r.turnoverRate.toFixed(2)}%</span>
                <span class="weibi-val" style="color:${r.weibi >= 0 ? '#dc2626' : '#16a34a'};font-weight:600;">${r.weibi >= 0 ? '+' : ''}${r.weibi.toFixed(2)}%</span>
                <span class="score-cell" style="color:${r.score >= 60 ? '#dc2626' : r.score >= 40 ? '#f59e0b' : '#16a34a'}">${r.score}<br><span class="${tagClass}">${tagText}</span></span>
            </div>
        `;
    });

    html += '</div>';
    scanResults.innerHTML = html;
}

function updateScanLastUpdateTime() {
    const timeEl = document.getElementById('scanLastUpdateTime');
    if (timeEl) {
        const now = new Date();
        timeEl.textContent = now.toLocaleTimeString('zh-CN', { hour12: false });
    }
}

function stopAutoScan() {
    if (autoScanInterval) {
        clearTimeout(autoScanInterval);
        autoScanInterval = null;
    }
}

// ========== 智能推荐功能 ==========
let todayRecommendations = [];
let isGeneratingRecommend = false;

// 计算买入点和卖出点
function calculateBuySellPoints(data) {
    const price = data.priceRaw || parseFloat(data.price);
    const high = parseFloat(data.high);
    const low = parseFloat(data.low);
    
    // 基于支撑阻力位计算买入点：现价的-1.5% ~ -2.5%
    const buyPoint = (price * (1 - 0.02)).toFixed(2);
    // 卖出点：现价的+3% ~ +5%
    const sellPoint = (price * (1 + 0.04)).toFixed(2);
    
    return {
        buy: buyPoint,
        current: price.toFixed(2),
        sell: sellPoint
    };
}

// 生成推荐理由
function generateRecommendReason(data, scoreResult) {
    const reasons = [];
    const changePercent = data.changePercent || 0;
    
    // 2026-06-11 优化：增加涨停股龙头逻辑分析
    if (changePercent > 9.5) {
        reasons.push(data.weibi > 80 ? '强势封板，封单厚实，龙头潜力初现' : '成功封板，建议次日观察集合竞价溢价');
    } else if (changePercent > 6) {
        reasons.push('多头意愿极强，正处于加速拉升区间');
    }
    
    if (data.volumeRatio > 1.2) reasons.push('量比放大(' + data.volumeRatio.toFixed(2) + ')，资金关注');
    if (data.turnoverRate > 2 && data.turnoverRate < 8) reasons.push('换手率适中(' + data.turnoverRate.toFixed(2) + '%)，交投活跃');
    if (data.weibi > 0) reasons.push('委比为正(' + data.weibi.toFixed(2) + '%)，买盘强势');
    
    const avgDev = data.avgPriceDeviation || 0;
    if (avgDev > 0 && avgDev < 2) reasons.push('价格位于均价附近，走势稳健');
    
    const outerRatio = data.outerRatio || (data.outerPlateRaw && data.innerPlateRaw ? ((data.outerPlateRaw / (data.outerPlateRaw + data.innerPlateRaw)) * 100).toFixed(1) : 50);
    if (outerRatio > 55) reasons.push('外盘占比(' + outerRatio + '%)高于内盘，主动买入较多');
    
    if (reasons.length === 0) {
        reasons.push('综合评分' + scoreResult.score + '分，符合' + scoreResult.label + '特征');
    }
    
    return '📊 ' + reasons.join('；');
}

// 扫描股票池并生成推荐（使用服务端批量API）
async function generateRecommendations() {
    if (isGeneratingRecommend) return;
    isGeneratingRecommend = true;
    
    const genBtn = document.getElementById('genRecommendBtn');
    const saveBtn = document.getElementById('saveRecommendBtn');
    const todayEl = document.getElementById('todayRecommend');
    const dateEl = document.getElementById('recommendDate');
    
    genBtn.disabled = true;
    genBtn.textContent = '🔄 批量获取中...';
    todayEl.innerHTML = '<div class="loading-card"><div class="loading-spinner"></div><div style="font-size:1rem;font-weight:500;color:#64748b;">正在通过服务端批量获取实时行情...</div></div>';
    
    try {
        const pool = window.scanStockPool || [];
        if (pool.length === 0) {
            throw new Error('股票池为空');
        }
        
        // 使用服务端批量扫描API，一次性获取全部股票数据并评分
        const response = await fetch(`/api/hot-stocks-scan?codes=${pool.join(',')}`);
        if (!response.ok) throw new Error('API请求失败: ' + response.status);
        
        const scanResults = await response.json();
        
        if (scanResults.error) {
            throw new Error(scanResults.error);
        }
        
        if (!Array.isArray(scanResults) || scanResults.length === 0) {
            throw new Error('未获取到有效数据，请稍后重试');
        }
        
        // 2026-06-11 策略优化：取消所有涨幅硬性过滤，支持捕捉龙头股和涨停股
        const filteredResults = scanResults.filter(r => {
            // 1. 仅保留对 ST 或退市风险股的基本过滤（为了基本面安全）
            if (r.name.includes('ST') || r.name.includes('退')) return false;
            
            // 2. 彻底取消涨幅限制，即便 10% 涨停也保留，用于发掘次日溢价机会
            return true;
        });

        if (filteredResults.length === 0) {
            throw new Error('当前市场环境下未筛选出符合严格安全条件的股票，请稍后再试');
        }

        // 取评分前3
        const top3 = filteredResults.slice(0, 3);
        
        todayRecommendations = top3.map(r => {
            const buySellPoints = calculateBuySellPoints(r);
            const reason = generateRecommendReason(r, { score: r.score, label: r.scoreLabel || '' });
            return {
                name: r.name,
                symbol: r.symbol,
                price: r.price,
                priceRaw: parseFloat(r.price),
                change: r.change,
                changePercent: r.changePercent,
                score: r.score,
                buySellPoints: buySellPoints,
                reason: reason
            };
        });
        
        renderTodayRecommendations();
        
        const now = new Date();
        dateEl.textContent = now.toLocaleDateString('zh-CN');
        
        saveBtn.style.display = 'inline-block';
        
    } catch (e) {
        console.error('生成推荐失败:', e);
        todayEl.innerHTML = '<div style="text-align:center;padding:2rem;color:#dc2626;">生成推荐失败：' + escapeHtml(e.message) + '</div>';
    } finally {
        genBtn.disabled = false;
        genBtn.textContent = '🎯 生成今日推荐';
        isGeneratingRecommend = false;
    }
}

function renderTodayRecommendations() {
    const todayEl = document.getElementById('todayRecommend');
    if (todayRecommendations.length === 0) {
        todayEl.innerHTML = '<div style="text-align:center;padding:4rem 2rem;color:#94a3b8;background:#f8fafc;border-radius:16px;border:2px dashed #e2e8f0;margin:1rem 0;">✨ 准备就绪，点击上方按钮生成今日潜力股</div>';
        return;
    }
    
    let html = '';
    todayRecommendations.forEach((r, idx) => {
        const isUp = r.change >= 0;
        const changeClass = isUp ? 'color:var(--accent-danger);' : 'color:var(--accent-success);';
        const changePrefix = isUp ? '+' : '';
        const rankIcon = idx === 0 ? '🥇' : idx === 1 ? '🥈' : '🥉';
        const scoreColor = r.score >= 70 ? 'var(--accent-danger)' : r.score >= 50 ? 'var(--accent-warning)' : 'var(--accent-success)';
        
        html += `
            <div class="recommend-card top-${idx + 1}">
                <div class="recommend-header">
                    <div class="recommend-rank-badge">${rankIcon}</div>
                    <div class="recommend-info-main">
                        <div class="recommend-name-row">
                            <span class="recommend-name-text">${escapeHtml(r.name)}</span>
                            <span class="recommend-code-text">${r.symbol.toUpperCase()}</span>
                        </div>
                    </div>
                    <div class="recommend-price-large" style="${changeClass}">${r.price}</div>
                </div>
                
                <div class="recommend-stats-grid">
                    <div class="recommend-stat-box">
                        <span class="recommend-stat-label">今日涨跌</span>
                        <span class="recommend-stat-value" style="${changeClass}">${changePrefix}${r.changePercent.toFixed(2)}%</span>
                    </div>
                    <div class="recommend-stat-box">
                        <span class="recommend-stat-label">AI 综合评分</span>
                        <span class="recommend-stat-value" style="color:${scoreColor}">${r.score} 分</span>
                    </div>
                </div>
                
                <div class="recommend-points-row">
                    <div class="recommend-point-card buy">
                        <div class="recommend-point-label">买入参考</div>
                        <div class="recommend-point-value" style="color:var(--accent-success)">${r.buySellPoints.buy}</div>
                    </div>
                    <div class="recommend-point-card current">
                        <div class="recommend-point-label">推荐时价</div>
                        <div class="recommend-point-value" style="color:var(--accent-primary)">${r.buySellPoints.current}</div>
                    </div>
                    <div class="recommend-point-card sell">
                        <div class="recommend-point-label">目标卖点</div>
                        <div class="recommend-point-value" style="color:var(--accent-danger)">${r.buySellPoints.sell}</div>
                    </div>
                </div>
                
                <div class="recommend-reason-box">
                    <div class="recommend-reason-text">${r.reason}</div>
                </div>
            </div>
        `;
    });
    
    todayEl.innerHTML = html;
}

// 保存推荐记录到后端
async function saveRecommendations() {
    if (todayRecommendations.length === 0) {
        showNotification('请先生成推荐');
        return;
    }
    
    const now = new Date();
    const dateKey = now.toISOString().split('T')[0];
    const record = {
        date: dateKey,
        daily_recommendations: todayRecommendations.map(r => ({
            name: r.name,
            symbol: r.symbol,
            price: r.price,
            change: r.change,
            changePercent: r.changePercent,
            score: r.score,
            buySellPoints: r.buySellPoints,
            reason: r.reason
        }))
    };
    
    try {
        const response = await fetch('/api/daily_recommendations', {
            method: 'POST',
            headers: {
                'Content-Type': 'application/json'
            },
            body: JSON.stringify(record)
        });
        
        const result = await response.json();
        
        if (result.success) {
            // 更新历史记录显示
            await renderHistoryRecommendations();
            showNotification('保存成功！推荐记录已保存到数据库');
        } else {
            showNotification('保存失败：' + (result.error || '未知错误'));
        }
    } catch (error) {
        console.error('保存推荐记录失败:', error);
        showNotification('保存失败：网络错误，请检查服务器是否正常运行');
    }
}

// 从后端加载并显示历史推荐记录
let recommendHistoryData = [];
let recommendHistoryPage = 1;
let recommendHistoryPages = [];

async function renderHistoryRecommendations() {
    const historyEl = document.getElementById('historyRecommend');
    
    try {
        const response = await fetch('/api/daily_recommendations');
        const result = await response.json();
        
        if (!result.success || !result.data || result.data.length === 0) {
            historyEl.innerHTML = '<div style="text-align:center;padding:2rem;color:#64748b;">暂无历史记录</div>';
            return;
        }
        
        historyEl.innerHTML = '<div style="text-align:center;padding:2rem;color:#64748b;">📊 正在加载最新价格...</div>';
        
        // 处理每个记录
        const processedRecords = await Promise.all(result.data.map(async (record) => {
            // 获取每个推荐股票的当前价格
            const processedRecommendations = await Promise.all(record.daily_recommendations.map(async (r) => {
                let currentPrice = null;
                let profitRatio = null;
                let profitClass = '';
                
                try {
                    // 调用API获取当前价格
                    const stockResp = await fetch(`/api/stock/${r.symbol}`);
                    if (stockResp.ok) {
                        const stockData = await stockResp.text();
                        const parsed = parseTencentData(stockData);
                        if (parsed) {
                            currentPrice = parsed.price;
                            const buyPrice = parseFloat(r.buySellPoints.buy);
                            const currPrice = parseFloat(currentPrice);
                            if (buyPrice > 0 && currPrice > 0) {
                                profitRatio = ((currPrice - buyPrice) / buyPrice * 100).toFixed(2);
                                profitClass = profitRatio >= 0 ? 'up' : 'down';
                            }
                        }
                    }
                } catch (e) {
                    console.log(`获取 ${r.symbol} 最新价格失败:`, e.message);
                }
                
                return { ...r, currentPrice, profitRatio, profitClass };
            }));
            
            return { ...record, daily_recommendations: processedRecommendations };
        }));
        
        recommendHistoryData = processedRecords;
        recommendHistoryPages = groupByWeek(processedRecords);
        recommendHistoryPage = 1;
        renderHistoryPage();
    } catch (error) {
        console.error('加载历史推荐记录失败:', error);
        historyEl.innerHTML = '<div style="text-align:center;padding:2rem;color:#dc2626;">加载历史记录失败，请检查服务器是否正常运行</div>';
    }
}

function groupByWeek(records) {
    if (!records || records.length === 0) return [];
    const pages = [];
    let currentWeek = null;
    let currentGroup = [];
    
    records.forEach(record => {
        const d = new Date(record.date);
        const dayOfWeek = d.getDay();
        const weekStart = new Date(d);
        weekStart.setDate(d.getDate() - (dayOfWeek === 0 ? 6 : dayOfWeek - 1));
        const weekKey = weekStart.toISOString().split('T')[0];
        
        if (weekKey !== currentWeek) {
            if (currentGroup.length > 0) {
                pages.push(currentGroup);
            }
            currentWeek = weekKey;
            currentGroup = [record];
        } else {
            currentGroup.push(record);
        }
    });
    
    if (currentGroup.length > 0) {
        pages.push(currentGroup);
    }
    
    return pages;
}

function renderHistoryPage() {
    const historyEl = document.getElementById('historyRecommend');
    if (!historyEl || recommendHistoryPages.length === 0) return;
    
    const totalPages = recommendHistoryPages.length;
    const pageData = recommendHistoryPages[recommendHistoryPage - 1] || [];
    
    let html = '';
    pageData.forEach(record => {
        html += `
            <div class="history-item">
                <div class="history-date-header">📅 ${record.date}</div>
                <div class="history-stock-grid">
                    ${record.daily_recommendations.map(r => {
                        const profitClass = r.profitRatio >= 0 ? 'up' : 'down';
                        const profitText = r.profitRatio !== null ? (r.profitRatio > 0 ? '+' + r.profitRatio : r.profitRatio) + '%' : '--';
                        return `
                                <div class="history-stock-card">
                                    <div class="history-stock-name-row">
                                        <div>
                                            <div style="font-weight:700;font-size:0.95rem;color:var(--text-primary);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:100px;">${escapeHtml(r.name)}</div>
                                            <div style="font-size:0.7rem;color:var(--text-tertiary);">${r.symbol.toUpperCase()}</div>
                                        </div>
                                        <div class="history-profit-badge ${profitClass}">${profitText}</div>
                                    </div>
                                    <div class="history-price-table">
                                        <span class="history-price-label">买入</span>
                                        <span class="history-price-val" style="color:var(--accent-danger)">${r.buySellPoints.buy}</span>
                                        <span class="history-price-label">现价</span>
                                        <span class="history-price-val">${r.currentPrice || '--'}</span>
                                        <span class="history-price-label">盈利</span>
                                        <span class="history-price-val" style="color:${parseFloat(profitText) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)'}">${profitText}</span>
                                    </div>
                                    <button class="btn-secondary" style="width:100%;padding:4px;font-size:0.7rem;border-radius:4px;background:#f1f5f9;border:none;" onclick="openKlineModal('${r.symbol}')">
                                        📊 K线
                                    </button>
                                </div>
                            `;
                    }).join('')}
                </div>
            </div>
        `;
    });
    
    if (totalPages > 1) {
        html += `<div class="history-pagination">
            <button class="page-btn" onclick="recommendHistoryPage=Math.max(1,recommendHistoryPage-1);renderHistoryPage();" ${recommendHistoryPage <= 1 ? 'disabled' : ''}>上一页</button>
            <span class="page-info">第 ${recommendHistoryPage} / ${totalPages} 页</span>
            <button class="page-btn" onclick="recommendHistoryPage=Math.min(${totalPages},recommendHistoryPage+1);renderHistoryPage();" ${recommendHistoryPage >= totalPages ? 'disabled' : ''}>下一页</button>
        </div>`;
    }
    
    historyEl.innerHTML = html;
}

// Tab切换功能
function initTabs() {
    const tabBtns = document.querySelectorAll('.tab-btn');
    const tabContents = document.querySelectorAll('.tab-content');
    
    tabBtns.forEach(btn => {
        btn.addEventListener('click', () => {
            const targetTab = btn.getAttribute('data-tab');
            
            // 移除所有active
            tabBtns.forEach(b => b.classList.remove('active'));
            tabContents.forEach(c => c.classList.remove('active'));
            
            // 添加active
            btn.classList.add('active');
            document.getElementById(targetTab).classList.add('active');
            
            // 切换到股票筛选时自动开始扫描和刷新
            if (targetTab === 'tab-scan') {
                if (!isScanning && !hasScanResult) {
                    startScan(false);
                    scheduleScanRefresh();
                } else if (!isScanning) {
                    scheduleScanRefresh();
                }
            } else {
                stopAutoScan();
            }
            // 切换到趋势发现时先加载缓存数据，再后台扫描
            if (targetTab === 'tab-trend') {
                if (!hasTrendData) {
                    refreshTrendStocks().then(() => {
                        hasTrendData = true;
                        if (trendStocks.length === 0) {
                            const trendResultsEl = document.getElementById('trendResults');
                            if (trendResultsEl) {
                                trendResultsEl.innerHTML = '<div class="loading-card"><div class="loading-spinner"></div><div style="font-size:1rem;font-weight:500;color:#64748b;">正在扫描趋势股票，请稍候...</div></div>';
                            }
                            scanTrendStocks();
                        } else {
                            checkAndRescanIfNeeded();
                        }
                    });
                } else {
                    scheduleTrendAutoRefresh();
                }
                scheduleTrendAutoRefresh();
            } else {
                stopTrendAutoRefresh();
            }
        });
    });
}

// ========== 趋势发现功能 ==========
let trendStocks = [];
let isScanningTrend = false;
let trendAutoRefreshTimer = null;
let hasTrendData = false;

// 存储展开状态
window.trendDetailsExpanded = {};

// 渲染趋势股票列表
function renderTrendStocks() {
    const trendResultsEl = document.getElementById('trendResults');
    const trendStatsEl = document.getElementById('trendStats');
    
    if (!trendStocks || trendStocks.length === 0) {
        trendResultsEl.innerHTML = '<div style="text-align:center;padding:2rem;color:#64748b;">暂无趋势股票，正在自动扫描中...</div>';
        if (trendStatsEl) trendStatsEl.textContent = '';
        return;
    }
    
    // 过滤掉已经在股票池中的股票，以及没有名称或名称为代码的股票
    const poolSymbols = (window.scanStockPool || []).map(s => s.toLowerCase());
    const filteredStocks = trendStocks.filter(stock => {
        const isNotInPool = !poolSymbols.includes(stock.symbol.toLowerCase());
        const hasValidName = stock.name && stock.name !== stock.symbol;
        return isNotInPool && hasValidName;
    });
    
    if (trendStatsEl) {
        trendStatsEl.textContent = `发现 ${filteredStocks.length} 只新趋势股票 (已过滤已在池中的)`;
    }
    
    if (filteredStocks.length === 0) {
        trendResultsEl.innerHTML = '<div class="trend-empty-state"><div class="trend-empty-icon">🔍</div><div>发现的趋势股票均已在股票池中</div></div>';
        return;
    }
    
    let html = '<div class="trend-card-list">';

    filteredStocks.forEach((stock, index) => {
        const scoreColor = stock.score >= 80 ? '#dc2626' : stock.score >= 60 ? '#f59e0b' : '#16a34a';
        const scoreBg = stock.score >= 80 ? 'linear-gradient(135deg,#fef2f2,#fee2e2)' : stock.score >= 60 ? 'linear-gradient(135deg,#fffbeb,#fef3c7)' : 'linear-gradient(135deg,#f0fdf4,#dcfce7)';
        const scoreBorder = stock.score >= 80 ? '#fca5a5' : stock.score >= 60 ? '#fde68a' : '#bbf7d0';
        const consecutiveDays = stock.details?.consecutiveUpDays || 0;
        const isExpanded = window.trendDetailsExpanded[stock.symbol];
        const changePct = stock.latestPrice && stock.details?.ma5 ? ((stock.latestPrice - stock.details.ma5[stock.details.ma5.length-1]) / stock.details.ma5[stock.details.ma5.length-1] * 100) : 0;
        
        html += `
            <div class="trend-card" data-symbol="${stock.symbol}">
                <div class="trend-card-main" onclick="toggleTrendDetails('${stock.symbol}')">
                    <div class="trend-card-left">
                        <div class="trend-card-name">${escapeHtml(stock.name)}</div>
                        <div class="trend-card-code">${stock.symbol.toUpperCase()}</div>
                    </div>
                    <div class="trend-card-price">
                        <span class="trend-price-val">${stock.latestPrice}</span>
                    </div>
                    <div class="trend-card-consecutive">
                        <span class="trend-consecutive-badge ${consecutiveDays >= 3 ? 'hot' : consecutiveDays >= 1 ? 'warm' : 'cool'}">${consecutiveDays}天</span>
                    </div>
                    <div class="trend-card-score">
                        <div class="trend-score-ring" style="background:${scoreBg};border-color:${scoreBorder};">
                            <span style="color:${scoreColor};font-size:1rem;font-weight:800;">${stock.score}</span>
                        </div>
                    </div>
                    <div class="trend-card-expand-icon ${isExpanded ? 'expanded' : ''}">
                        <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M4 6L8 10L12 6" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>
                    </div>
                </div>
                <div class="trend-card-detail ${isExpanded ? 'open' : ''}" id="trend-detail-${stock.symbol}">
                    <div class="trend-detail-inner">
                        ${renderTrendDetails(stock.details, stock.symbol, isExpanded)}
                    </div>
                    <div class="trend-card-actions">
                        <button class="trend-btn-kline" onclick="openKlineModal('${stock.symbol}');event.stopPropagation();">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>
                            K线
                        </button>
                        <button class="trend-btn-add" onclick="addToScanPool('${stock.symbol}');event.stopPropagation();">
                            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
                            添加
                        </button>
                    </div>
                </div>
            </div>
        `;
    });

    html += '</div>';
    trendResultsEl.innerHTML = html;
}

// 切换详情展开状态
function toggleTrendDetails(symbol) {
    window.trendDetailsExpanded[symbol] = !window.trendDetailsExpanded[symbol];
    renderTrendStocks();
}

// 渲染趋势详情
function renderTrendDetails(details, symbol, isExpanded) {
    if (!details) return '<div class="trend-detail-no-data">暂无数据</div>';
    
    const conds = [
        { label: '站上120线', ok: details.priceAboveMa120, icon: '📈' },
        { label: '120线向上', ok: details.ma120SlopeUp, icon: '↗️' },
        { label: '60在120上', ok: details.ma60AboveMa120, icon: '⬆️' },
        { label: '120日涨停', ok: details.limitUp120, icon: '🔥' },
        { label: '1年涨停', ok: details.limitUp250, icon: '🚀' },
        { label: '回撤<30%', ok: details.drawdown30, icon: '🛡️' }
    ];
    
    const okCount = conds.filter(c => c.ok).length;
    const condTags = conds.map(c => 
        `<span class="trend-cond-tag ${c.ok ? 'ok' : 'fail'}">${c.ok ? '✓' : '○'} ${c.label}</span>`
    ).join('');
    
    if (!isExpanded) {
        return `<div class="trend-detail-collapsed">
            <div class="trend-cond-summary">${okCount}/${conds.length} 条件满足</div>
            <div class="trend-cond-tags">${condTags}</div>
        </div>`;
    }
    
    const metrics = [
        { label: '连涨天数', value: `${details.consecutiveUpDays || 0}天`, icon: '📊' },
        { label: '120日涨停', value: `${details.limitUpCount120 || 0}次`, icon: '🔥' },
        { label: '1年涨停', value: `${details.limitUpCount250 || 0}次`, icon: '🚀' },
        { label: '最大回撤', value: `${details.maxDrawdown120 || 0}%`, icon: '📉' },
        { label: 'MA60', value: details.ma60 || '--', icon: '📏' },
        { label: 'MA120', value: details.ma120 || '--', icon: '📏' }
    ];
    
    const metricItems = metrics.map(m => 
        `<div class="trend-metric-item">
            <span class="trend-metric-label">${m.label}</span>
            <span class="trend-metric-value">${m.value}</span>
        </div>`
    ).join('');
    
    return `<div class="trend-detail-expanded">
        <div class="trend-cond-section">
            <div class="trend-section-label">趋势条件 <span class="trend-cond-count">${okCount}/${conds.length}</span></div>
            <div class="trend-cond-tags">${condTags}</div>
        </div>
        <div class="trend-metric-section">
            <div class="trend-section-label">关键指标</div>
            <div class="trend-metric-grid">${metricItems}</div>
        </div>
    </div>`;
}

// 扫描趋势股票
async function scanTrendStocks() {
    if (isScanningTrend) return;
    isScanningTrend = true;
    
    const trendResultsEl = document.getElementById('trendResults');
    const trendLastScanEl = document.getElementById('trendLastScan');
    
    trendResultsEl.innerHTML = '<div class="loading-card"><div class="loading-spinner"></div><div style="font-size:1rem;font-weight:500;color:#64748b;">正在扫描趋势股票...</div></div>';
    
    try {
        const response = await fetch('/api/scan-trend-stocks');
        const result = await response.json();
        
        if (!result.success) {
            throw new Error(result.error || '扫描失败');
        }
        
        trendStocks = result.data.stocks || [];
        
        if (trendLastScanEl) {
            const today = new Date().toISOString().split('T')[0];
            trendLastScanEl.textContent = `${today} · ${result.data.scanTime}`;
        }
        
        renderTrendStocks();
        
    } catch (e) {
        console.error('扫描趋势股票失败:', e);
        trendResultsEl.innerHTML = '<div style="text-align:center;padding:2rem;color:#dc2626;">扫描失败：' + escapeHtml(e.message) + '</div>';
    } finally {
        isScanningTrend = false;
    }
}

// 刷新趋势数据
async function refreshTrendStocks() {
    const trendResultsEl = document.getElementById('trendResults');
    
    try {
        const response = await fetch('/api/trend-stocks');
        const result = await response.json();
        
        if (!result.success) {
            throw new Error(result.error || '获取失败');
        }
        
        trendStocks = result.data.stocks || [];
        
        const trendLastScanEl = document.getElementById('trendLastScan');
        if (trendLastScanEl) {
            const today = new Date().toISOString().split('T')[0];
            trendLastScanEl.textContent = today;
        }
        
        renderTrendStocks();
        
    } catch (e) {
        console.error('刷新趋势数据失败:', e);
    }
}

async function checkAndRescanIfNeeded() {
    try {
        const response = await fetch('/api/trend-stocks');
        const result = await response.json();
        if (result.success && result.data) {
            const today = new Date().toISOString().split('T')[0];
            const scanDate = result.data.scanDate || result.data.date || '';
            if (scanDate !== today) {
                console.log(`[趋势] 数据过期（${scanDate}），后台重新扫描...`);
                scanTrendStocks();
                return;
            }
            if (result.data.stocks && result.data.stocks.length > 0) {
                trendStocks = result.data.stocks;
                renderTrendStocks();
                hasTrendData = true;
                const trendLastScanEl = document.getElementById('trendLastScan');
                if (trendLastScanEl) {
                    trendLastScanEl.textContent = today;
                }
                return;
            }
        }
        if (!trendStocks || trendStocks.length === 0) {
            console.log('[趋势] 无缓存数据，后台重新扫描...');
            scanTrendStocks();
        }
    } catch (e) {
        console.error('检查趋势数据失败:', e);
    }
}

function scheduleTrendAutoRefresh() {
    stopTrendAutoRefresh();
    const refreshOnce = async () => {
        try {
            if (!isScanningTrend) {
                await refreshTrendStocks();
                hasTrendData = true;
            }
        } catch (e) {
            console.error('趋势自动刷新失败:', e);
        }
        const nextDelay = isTradingTime() ? 30000 : 300000;
        trendAutoRefreshTimer = setTimeout(refreshOnce, nextDelay);
    };
    refreshOnce();
}

function stopTrendAutoRefresh() {
    if (trendAutoRefreshTimer) {
        clearTimeout(trendAutoRefreshTimer);
        trendAutoRefreshTimer = null;
    }
}

// ========== AI 诊断功能 ==========
let aiDiagnosing = false;

async function startAiDiagnose(symbol) {
    if (aiDiagnosing) return;
    
    const btn = document.getElementById('aiDiagnoseBtn');
    if (!btn) return;
    
    const configResp = await fetch('/api/ai-config');
    const configResult = await configResp.json();
    
    if (!configResult.success || !configResult.data.configured) {
        alert('请先在 server/ai_config.json 中配置 API Key');
        return;
    }
    
    aiDiagnosing = true;
    btn.disabled = true;
    btn.innerHTML = '<span style="font-size:1.1rem;">⏳</span> 诊断中...';
    
    showAiDiagModal('loading');
    
    try {
        const response = await fetch(`/api/ai-diagnose/${symbol}`);
        const result = await response.json();
        
        if (!result.success) {
            throw new Error(result.detail || '诊断失败');
        }
        
        renderAiDiagnosis(result.data);
    } catch (e) {
        showAiDiagModal('error', e.message);
    } finally {
        aiDiagnosing = false;
        btn.disabled = false;
        btn.innerHTML = '<span style="font-size:1.1rem;">🤖</span> AI诊断';
    }
}

function showAiDiagModal(type, errorMsg) {
    let existing = document.getElementById('aiDiagModal');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'aiDiagModal';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:10000;display:flex;align-items:center;justify-content:center;animation:fadeIn 0.2s ease;';

    const modal = document.createElement('div');
    modal.style.cssText = 'background:#fff;border-radius:16px;width:90%;max-width:520px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,0.3);animation:slideUp 0.3s ease;';

    if (type === 'loading') {
        modal.innerHTML = `
            <div style="padding:20px 24px;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;justify-content:space-between;">
                <div style="display:flex;align-items:center;gap:8px;">
                    <span style="font-size:1.3rem;">🤖</span>
                    <span style="font-size:1.1rem;font-weight:700;color:#1e293b;">AI诊断</span>
                </div>
                <button onclick="closeAiDiagModal()" style="background:none;border:none;font-size:1.5rem;cursor:pointer;color:#94a3b8;padding:0 4px;line-height:1;">&times;</button>
            </div>
            <div style="padding:40px 24px;text-align:center;">
                <div class="loading-spinner"></div>
                <div style="font-size:0.95rem;color:#64748b;margin-top:12px;">AI正在分析中，请稍候...</div>
            </div>
        `;
    } else if (type === 'error') {
        modal.innerHTML = `
            <div style="padding:20px 24px;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;justify-content:space-between;">
                <div style="display:flex;align-items:center;gap:8px;">
                    <span style="font-size:1.3rem;">🤖</span>
                    <span style="font-size:1.1rem;font-weight:700;color:#1e293b;">AI诊断</span>
                </div>
                <button onclick="closeAiDiagModal()" style="background:none;border:none;font-size:1.5rem;cursor:pointer;color:#94a3b8;padding:0 4px;line-height:1;">&times;</button>
            </div>
            <div style="padding:30px 24px;text-align:center;">
                <div style="font-size:2rem;margin-bottom:8px;">❌</div>
                <div style="font-size:1rem;font-weight:600;color:#dc2626;margin-bottom:6px;">诊断失败</div>
                <div style="font-size:0.85rem;color:#64748b;">${escapeHtml(errorMsg || '未知错误')}</div>
            </div>
        `;
    }

    overlay.appendChild(modal);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeAiDiagModal(); });
    document.body.appendChild(overlay);
}

function closeAiDiagModal() {
    const modal = document.getElementById('aiDiagModal');
    if (modal) modal.remove();
}

function renderAiDiagnosis(data) {
    const diag = data.diagnosis || {};
    const stock = data.stock || {};
    
    const direction = diag.direction || '观望';
    const confidence = diag.confidence || 50;
    const summary = diag.summary || '';
    const scores = diag.scores || {};
    const analysis = diag.analysis || {};
    const keySignals = diag.keySignals || {};
    const triggerCondition = diag.triggerCondition || '';
    const risk = diag.risk || '';
    const suggestion = diag.suggestion || '';
    
    const dirConfig = {
        '强烈买入': { color: '#dc2626', bg: '#fef2f2', border: '#fca5a5', icon: '🔥' },
        '买入': { color: '#dc2626', bg: '#fef2f2', border: '#fecaca', icon: '📈' },
        '轻仓关注': { color: '#ea580c', bg: '#fff7ed', border: '#fed7aa', icon: '👀' },
        '观望': { color: '#d97706', bg: '#fffbeb', border: '#fde68a', icon: '⏸️' },
        '减仓': { color: '#16a34a', bg: '#f0fdf4', border: '#bbf7d0', icon: '🔽' },
        '卖出': { color: '#16a34a', bg: '#f0fdf4', border: '#86efac', icon: '📉' }
    };
    const dc = dirConfig[direction] || dirConfig['观望'];
    
    const scoreItems = [
        { label: '成交量', key: 'volume', icon: '📊' },
        { label: '主力资金', key: 'capital', icon: '💰' },
        { label: '技术面', key: 'technique', icon: '📈' },
        { label: '大盘环境', key: 'market', icon: '🌍' },
        { label: '基本面', key: 'fundamental', icon: '📋' }
    ].filter(item => scores[item.key] !== undefined);

    const analysisItems = [
        { label: '📊 成交量', value: analysis.volume },
        { label: '💰 主力资金', value: analysis.capital },
        { label: '📈 技术面', value: analysis.technique },
        { label: '🌍 大盘环境', value: analysis.market },
        { label: '📋 基本面', value: analysis.fundamental }
    ].filter(item => item.value);

    const bullishSignals = keySignals.bullish || [];
    const bearishSignals = keySignals.bearish || [];

    const stockName = stock.name || '';
    const stockPrice = stock.price ? ` ${stock.price}元` : '';
    const changeInfo = stock.changePercent ? ` (${stock.changePercent > 0 ? '+' : ''}${stock.changePercent}%)` : '';

    let existing = document.getElementById('aiDiagModal');
    if (existing) existing.remove();

    const overlay = document.createElement('div');
    overlay.id = 'aiDiagModal';
    overlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,0.5);z-index:10000;display:flex;align-items:center;justify-content:center;animation:fadeIn 0.2s ease;';

    const modal = document.createElement('div');
    modal.style.cssText = 'background:#fff;border-radius:16px;width:90%;max-width:560px;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,0.3);animation:slideUp 0.3s ease;';

    modal.innerHTML = `
        <div style="padding:20px 24px;border-bottom:1px solid #f1f5f9;display:flex;align-items:center;justify-content:space-between;">
            <div style="display:flex;align-items:center;gap:8px;">
                <span style="font-size:1.3rem;">🤖</span>
                <span style="font-size:1.1rem;font-weight:700;color:#1e293b;">AI诊断</span>
                ${stockName ? `<span style="font-size:0.85rem;color:#64748b;margin-left:4px;">${escapeHtml(stockName)}${stockPrice}${changeInfo}</span>` : ''}
            </div>
            <button onclick="closeAiDiagModal()" style="background:none;border:none;font-size:1.5rem;cursor:pointer;color:#94a3b8;padding:0 4px;line-height:1;">&times;</button>
        </div>
        <div style="padding:16px 24px;overflow-y:auto;flex:1;">
            <div style="border:2px solid ${dc.border};border-radius:12px;overflow:hidden;background:${dc.bg};">
                <div style="display:flex;align-items:center;justify-content:space-between;padding:14px 16px;border-bottom:1px solid ${dc.border};">
                    <div style="display:flex;align-items:center;gap:8px;">
                        <span style="font-size:1.5rem;">${dc.icon}</span>
                        <span style="font-size:1.3rem;font-weight:700;color:${dc.color};">${direction}</span>
                    </div>
                    <div style="display:flex;align-items:center;gap:6px;">
                        <span style="font-size:0.8rem;color:#64748b;">置信度</span>
                        <span style="font-size:1.1rem;font-weight:700;color:${dc.color};">${confidence}%</span>
                    </div>
                </div>
                <div style="padding:14px 16px;">
                    <div style="font-size:0.9rem;font-weight:600;color:#1e293b;margin-bottom:12px;">${escapeHtml(summary)}</div>
                    ${scoreItems.length > 0 ? `
                        <div style="margin-bottom:12px;">
                            <div style="font-size:0.8rem;font-weight:600;color:#475569;margin-bottom:8px;">📐 维度评分</div>
                            <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;">
                                ${scoreItems.map(item => {
                                    const s = scores[item.key];
                                    const barColor = s >= 60 ? '#22c55e' : s >= 40 ? '#f59e0b' : '#ef4444';
                                    const barBg = s >= 60 ? '#dcfce7' : s >= 40 ? '#fef3c7' : '#fee2e2';
                                    return `<div style="display:flex;align-items:center;gap:6px;padding:6px 8px;background:rgba(255,255,255,0.7);border-radius:6px;">
                                        <span style="font-size:0.75rem;color:#64748b;white-space:nowrap;">${item.icon} ${item.label}</span>
                                        <div style="flex:1;height:6px;background:#e2e8f0;border-radius:3px;overflow:hidden;">
                                            <div style="width:${s}%;height:100%;background:${barColor};border-radius:3px;"></div>
                                        </div>
                                        <span style="font-size:0.75rem;font-weight:600;color:${barColor};">${s}</span>
                                    </div>`;
                                }).join('')}
                            </div>
                        </div>
                    ` : ''}
                    ${(bullishSignals.length > 0 || bearishSignals.length > 0) ? `
                        <div style="margin-bottom:12px;">
                            <div style="font-size:0.8rem;font-weight:600;color:#475569;margin-bottom:8px;">📡 多空信号</div>
                            <div style="display:flex;gap:8px;">
                                ${bullishSignals.length > 0 ? `<div style="flex:1;padding:8px 10px;background:rgba(34,197,94,0.08);border-radius:8px;border:1px solid rgba(34,197,94,0.2);">
                                    <div style="font-size:0.75rem;font-weight:600;color:#16a34a;margin-bottom:4px;">🟢 看多</div>
                                    ${bullishSignals.map(s => `<div style="font-size:0.78rem;color:#15803d;line-height:1.5;">• ${escapeHtml(s)}</div>`).join('')}
                                </div>` : ''}
                                ${bearishSignals.length > 0 ? `<div style="flex:1;padding:8px 10px;background:rgba(239,68,68,0.08);border-radius:8px;border:1px solid rgba(239,68,68,0.2);">
                                    <div style="font-size:0.75rem;font-weight:600;color:#dc2626;margin-bottom:4px;">🔴 看空</div>
                                    ${bearishSignals.map(s => `<div style="font-size:0.78rem;color:#b91c1c;line-height:1.5;">• ${escapeHtml(s)}</div>`).join('')}
                                </div>` : ''}
                            </div>
                        </div>
                    ` : ''}
                    <div style="font-size:0.8rem;font-weight:600;color:#475569;margin-bottom:8px;">📝 详细分析</div>
                    ${analysisItems.map(item => `
                        <div style="margin-bottom:8px;padding:8px 10px;background:rgba(255,255,255,0.7);border-radius:8px;">
                            <div style="font-size:0.8rem;font-weight:600;color:#475569;margin-bottom:3px;">${item.label}</div>
                            <div style="font-size:0.82rem;color:#334155;line-height:1.5;">${escapeHtml(item.value)}</div>
                        </div>
                    `).join('')}
                    ${triggerCondition ? `
                        <div style="margin-top:10px;padding:10px 12px;background:rgba(59,130,246,0.08);border-radius:8px;border-left:3px solid #3b82f6;">
                            <div style="font-size:0.8rem;font-weight:600;color:#2563eb;margin-bottom:3px;">🎯 转入条件</div>
                            <div style="font-size:0.82rem;color:#1e40af;line-height:1.5;">${escapeHtml(triggerCondition)}</div>
                        </div>
                    ` : ''}
                    ${suggestion ? `
                        <div style="margin-top:10px;padding:10px 12px;background:rgba(255,255,255,0.9);border-radius:8px;border-left:3px solid ${dc.color};">
                            <div style="font-size:0.8rem;font-weight:600;color:${dc.color};margin-bottom:3px;">💡 操作建议</div>
                            <div style="font-size:0.82rem;color:#334155;line-height:1.5;">${escapeHtml(suggestion)}</div>
                        </div>
                    ` : ''}
                    ${risk ? `
                        <div style="margin-top:8px;padding:8px 10px;background:rgba(254,242,242,0.7);border-radius:8px;">
                            <div style="font-size:0.8rem;font-weight:600;color:#dc2626;margin-bottom:3px;">⚠️ 风险提示</div>
                            <div style="font-size:0.82rem;color:#7f1d1d;line-height:1.5;">${escapeHtml(risk)}</div>
                        </div>
                    ` : ''}
                </div>
            </div>
        </div>
    `;

    overlay.appendChild(modal);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) closeAiDiagModal(); });
    document.body.appendChild(overlay);
}
function showNotification(message) {
    let toast = document.querySelector('.toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.className = 'toast';
        document.body.appendChild(toast);
    }
    
    toast.textContent = message;
    toast.classList.add('show');
    
    setTimeout(() => {
        toast.classList.remove('show');
    }, 3000);
}

// ========== 股票池管理功能 ==========

async function getFullNameFromTop500(symbol) {
    try {
        if (!window._top500Stocks) {
            const resp = await fetch('/api/top500');
            const json = await resp.json();
            if (json.success && json.data && json.data.stocks) {
                window._top500Stocks = json.data.stocks;
            } else {
                window._top500Stocks = [];
            }
        }
        const found = window._top500Stocks.find(s => s.symbol === symbol.toLowerCase());
        if (found && found.name) return found.name;
    } catch (e) {}
    return null;
}

// 添加股票到扫描池
async function addToScanPool(input) {
    if (!input) return;
    let symbol = input.toLowerCase().trim();
    
    // 如果输入的是中文名称，尝试从映射表查找代码
    const nameMap = getStockNameMap();
    if (nameMap[input.trim()]) {
        symbol = nameMap[input.trim()];
    } else if (!/^(sh|sz)\d{6}$/.test(symbol)) {
        // 如果不是标准代码格式，可能是纯数字，尝试补全
        if (/^\d{6}$/.test(symbol)) {
            if (symbol.startsWith('6')) symbol = 'sh' + symbol;
            else if (symbol.startsWith('0') || symbol.startsWith('3') || symbol.startsWith('00') || symbol.startsWith('30')) symbol = 'sz' + symbol;
            else {
                showNotification('无法识别的股票代码，请指定 sh 或 sz 前缀');
                return;
            }
        } else {
            showNotification('股票格式无效，请输入代码(如 sh603985) 或 完整名称');
            return;
        }
    }
    
    // 尝试获取股票名称以验证是否存在
    let resolvedName = null;
    try {
        const response = await fetch(`/api/stock/${symbol}`);
        const data = await response.text();
        if (!data || data.includes('pv_none_match')) {
            showNotification(`未找到股票 ${symbol}，请检查代码是否正确`);
            return;
        }
        
        // 提取名称并更新本地映射
        const match = data.match(/v_[\w]+="([^"]+)"/);
        if (match) {
            let name = match[1].split('~')[1];
            if (name) {
                const top500Name = await getFullNameFromTop500(symbol);
                if (top500Name && top500Name.length > name.length) {
                    name = top500Name;
                }

                // 过滤禁忌股票
                if (isForbiddenStock(name)) {
                    showNotification(`股票 ${name} 属于禁忌类别，不予添加`);
                    return;
                }

                resolvedName = name;
                if (!window.stockNameMap) window.stockNameMap = {};
                window.stockNameMap[symbol] = name;
                if (!window.stockSymbolMap) window.stockSymbolMap = {};
                window.stockSymbolMap[name] = symbol;
            }
        }
    } catch (e) {
        console.error('验证股票失败:', e);
    }
    
    if (!resolvedName) {
        showNotification(`未能获取 ${symbol} 的名称，请稍后重试`);
        return;
    }
    
    // 同步名称到服务器（必须 await，失败要回滚）
    try {
        const mapResp = await fetch('/api/update-stock-map', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol, name: resolvedName })
        });
        const mapResult = await mapResp.json();
        if (!mapResult.success) {
            showNotification(`保存名称失败：${mapResult.error || '未知错误'}，已取消添加`);
            return;
        }
    } catch (e) {
        console.error('同步映射表失败:', e);
        showNotification('同步名称到服务器失败，已取消添加');
        return;
    }
    
    // 添加到后端自定义扫描池
    try {
        const response = await fetch('/api/custom-scan-pool/add', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol: symbol })
        });
        const result = await response.json();
        if (result.success) {
            window.scanStockPool = result.data.symbols;
            const displayName = `${resolvedName}(${symbol})`;
            showNotification(`已添加 ${displayName} 到扫描池！`);
            renderScanPool();
            // 同时刷新趋势列表
            if (typeof renderTrendStocks === 'function') {
                renderTrendStocks();
            }
        } else {
            showNotification(result.message || '添加失败');
        }
    } catch (e) {
        console.error('添加到扫描池失败:', e);
        showNotification('添加失败，请稍后重试');
    }
}

// 从扫描池删除股票
async function removeFromScanPool(symbol) {
    try {
        const response = await fetch('/api/custom-scan-pool/remove', {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ symbol: symbol })
        });
        const result = await response.json();
        if (result.success) {
            window.scanStockPool = result.data.symbols;
            showNotification(`已删除 ${symbol}`);
            renderScanPool();
            // 同时刷新趋势列表
            if (typeof renderTrendStocks === 'function') {
                renderTrendStocks();
            }
        } else {
            showNotification(result.message || '删除失败');
        }
    } catch (e) {
        console.error('从扫描池删除失败:', e);
        showNotification('删除失败，请稍后重试');
    }
}

// 渲染扫描池
function renderScanPool() {
    const poolListEl = document.getElementById('scanPoolList');
    const poolStatsEl = document.getElementById('poolStats');
    
    if (!window.scanStockPool || window.scanStockPool.length === 0) {
        if (poolListEl) poolListEl.innerHTML = '<div style="text-align:center;padding:1.5rem;color:#64748b;">扫描池为空，请从上方添加</div>';
        if (poolStatsEl) poolStatsEl.textContent = '共 0 只股票';
        return;
    }
    
    if (poolStatsEl) {
        poolStatsEl.textContent = `共 ${window.scanStockPool.length} 只股票`;
    }
    
    const codeMap = getStockCodeMap();
    
    let html = '<div class="stock-table pool-table"><div class="stock-table-header">';
    html += '<span>股票代码</span><span>名称</span><span>操作</span>';
    html += '</div>';
    
    window.scanStockPool.forEach(symbol => {
        const lowerSymbol = symbol.toLowerCase();
        const name = codeMap[lowerSymbol] || '--';
        
        html += `
            <div class="stock-table-row">
                <span class="name">${symbol.toUpperCase()}</span>
                <span class="price" style="color:#1e293b;">${escapeHtml(name)}</span>
                <span style="display:flex;gap:0.4rem;flex-wrap:wrap;">
                    <button class="btn-secondary" style="font-size:0.8rem;padding:0.3rem 0.6rem;background-color:#dbeafe;color:#1e40af;border-color:#bfdbfe;" onclick="openKlineModal('${symbol}')">
                        📊 查看K线
                    </button>
                    <button class="btn-secondary" style="font-size:0.8rem;padding:0.3rem 0.6rem;background-color:#fee2e2;color:#991b1b;border-color:#fecaca;" onclick="removeFromScanPool('${symbol}')">
                        🗑️ 删除
                    </button>
                </span>
            </div>
        `;
    });
    
    html += '</div>';
    
    if (poolListEl) {
        poolListEl.innerHTML = html;
    }
}

// 从后端加载自定义扫描池
async function loadScanPoolFromAPI() {
    try {
        const response = await fetch('/api/custom-scan-pool');
        const result = await response.json();
        if (result.success && result.data && result.data.symbols) {
            window.scanStockPool = result.data.symbols;
            return true;
        }
    } catch (e) {
        console.error('从后端加载扫描池失败:', e);
    }
    // 如果加载失败，使用默认配置
    window.scanStockPool = getScanStockPool();
    return false;
}

// 检查并自动更新趋势数据
async function checkAndAutoUpdateTrend() {
    try {
        const response = await fetch('/api/trend-stocks');
        const result = await response.json();
        if (result.success && result.data) {
            trendStocks = result.data.stocks || [];
            if (result.data.date) {
                hasTrendData = true;
            }
            if (result.data.date) {
                const today = new Date().toISOString().split('T')[0];
                if (result.data.date !== today) {
                    console.log(`[趋势] 数据过期（${result.data.date}），后台重新扫描...`);
                    scanTrendStocks();
                    return;
                }
            }
            if (!trendStocks || trendStocks.length === 0) {
                console.log('[趋势] 无缓存数据，后台重新扫描...');
                scanTrendStocks();
            }
        }
    } catch (e) {
        console.error('检查趋势数据失败:', e);
    }
}

// 加载收评列表
let reviewData = [];
let reviewPage = 1;

async function loadDailyReviews() {
    try {
        const response = await fetch('/api/daily-reviews');
        const result = await response.json();
        if (result.success) {
            reviewData = result.data || [];
            reviewPage = 1;
            renderDailyReviews();
        }
    } catch (e) {
        console.error('加载收评失败:', e);
    }
}

function renderDailyReviews() {
    const container = document.getElementById('reviewContainer');
    if (!container) return;
    
    if (reviewData.length === 0) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:#64748b;">暂无收评，点击上方按钮生成今日收评</div>';
        return;
    }
    
    const totalPages = reviewData.length;
    const review = reviewData[reviewPage - 1];
    
    let html = renderReviewCard(review, reviewPage === 1);
    
    if (totalPages > 1) {
        html += `<div class="history-pagination">
            <button class="page-btn" onclick="reviewPage=Math.max(1,reviewPage-1);renderDailyReviews();" ${reviewPage <= 1 ? 'disabled' : ''}>上一页</button>
            <span class="page-info">第 ${reviewPage} / ${totalPages} 天</span>
            <button class="page-btn" onclick="reviewPage=Math.min(${totalPages},reviewPage+1);renderDailyReviews();" ${reviewPage >= totalPages ? 'disabled' : ''}>下一页</button>
        </div>`;
    }
    
    container.innerHTML = html;
}

// 渲染单个收评卡片
function renderReviewCard(review, isLatest) {
    const date = review.date;
    const market = review.market || {};
    const industrySectors = review.industrySectors || [];
    const conceptSectors = review.conceptSectors || [];
    const limitUp = review.limitUpStocks || [];
    const limitDown = review.limitDownStocks || [];
    const topGainers = review.topGainers || [];
    const topLosers = review.topLosers || [];
    const tomorrowFocus = review.tomorrowFocus || [];
    
    // 大盘走势
    let marketHtml = '';
    ['sh000001', 'sz399001', 'sz399006'].forEach(code => {
        if (market[code]) {
            const idx = market[code];
            const color = idx.changePercent > 0 ? '#dc2626' : idx.changePercent < 0 ? '#16a34a' : '#475569';
            const bg = idx.changePercent > 0 ? '#fef2f2' : idx.changePercent < 0 ? '#f0fdf4' : '#f8fafc';
            marketHtml += `
                <div style="text-align:center;padding:12px;background:${bg};border-radius:10px;flex:1;min-width:120px;">
                    <div style="font-size:0.8rem;color:#64748b;margin-bottom:4px;">${escapeHtml(idx.name)}</div>
                    <div style="font-size:1.2rem;font-weight:700;color:#1e293b;">${idx.price}</div>
                    <div style="font-size:0.95rem;font-weight:600;color:${color};">
                        ${idx.changePercent > 0 ? '+' : ''}${idx.changePercent}%
                    </div>
                </div>`;
        }
    });
    
    // 板块排行（行业+概念）
    function renderSectorList(sectors, title) {
        if (!sectors || sectors.length === 0) return '';
        const top5 = sectors.slice(0, 5);
        const bottom5 = sectors.slice(-5).reverse();
        let html = `<div style="margin-bottom:16px;">
            <div style="font-weight:600;font-size:0.9rem;color:#1e293b;margin-bottom:8px;">${title}</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">`;
        // 领涨
        html += `<div>
            <div style="font-size:0.75rem;color:#dc2626;font-weight:600;margin-bottom:4px;">领涨</div>`;
        top5.forEach(s => {
            html += `<div style="display:flex;justify-content:space-between;padding:4px 8px;background:#fef2f2;border-radius:6px;margin-bottom:3px;font-size:0.8rem;">
                <span style="color:#1e293b;">${escapeHtml(s.name)}</span>
                <span style="color:#dc2626;font-weight:600;">+${s.changePercent}%</span>
            </div>`;
        });
        html += '</div>';
        // 领跌
        html += `<div>
            <div style="font-size:0.75rem;color:#16a34a;font-weight:600;margin-bottom:4px;">领跌</div>`;
        bottom5.forEach(s => {
            html += `<div style="display:flex;justify-content:space-between;padding:4px 8px;background:#f0fdf4;border-radius:6px;margin-bottom:3px;font-size:0.8rem;">
                <span style="color:#1e293b;">${escapeHtml(s.name)}</span>
                <span style="color:#16a34a;font-weight:600;">${s.changePercent}%</span>
            </div>`;
        });
        html += '</div></div></div>';
        return html;
    }
    
    // 涨跌停个股
    function renderStockList(stocks, title, isUp) {
        if (!stocks || stocks.length === 0) return `<div style="color:#94a3b8;font-size:0.85rem;padding:8px;">暂无数据</div>`;
        const color = isUp ? '#dc2626' : '#16a34a';
        const bg = isUp ? '#fef2f2' : '#f0fdf4';
        let html = `<div style="font-weight:600;font-size:0.9rem;color:${color};margin-bottom:8px;">${title}（${stocks.length}只）</div>`;
        html += '<div style="display:flex;flex-wrap:wrap;gap:6px;">';
        stocks.forEach(s => {
            html += `<div style="padding:4px 10px;background:${bg};border-radius:6px;font-size:0.8rem;border:1px solid ${isUp ? '#fecaca' : '#bbf7d0'};">
                <span style="color:#1e293b;font-weight:500;">${escapeHtml(s.name)}</span>
                <span style="color:${color};font-weight:600;margin-left:4px;">${s.changePercent > 0 ? '+' : ''}${s.changePercent}%</span>
            </div>`;
        });
        html += '</div>';
        return html;
    }
    
    // 涨跌幅排行
    function renderRankList(stocks, title, isUp) {
        if (!stocks || stocks.length === 0) return '';
        const color = isUp ? '#dc2626' : '#16a34a';
        let html = `<div style="margin-bottom:16px;">
            <div style="font-weight:600;font-size:0.9rem;color:#1e293b;margin-bottom:8px;">${title}</div>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px;">`;
        stocks.slice(0, 10).forEach(s => {
            const bg = isUp ? '#fef2f2' : '#f0fdf4';
            html += `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 10px;background:${bg};border-radius:8px;">
                <span style="font-size:0.85rem;color:#1e293b;font-weight:500;">${escapeHtml(s.name)}</span>
                <span style="font-size:0.85rem;color:${color};font-weight:600;">${s.changePercent > 0 ? '+' : ''}${s.changePercent}%</span>
            </div>`;
        });
        html += '</div></div>';
        return html;
    }
    
    // 明日关注
    let focusHtml = '';
    if (tomorrowFocus.length > 0) {
        focusHtml = `<div style="margin-top:16px;padding:16px;background:linear-gradient(135deg,#fefce8,#fef3c7);border-radius:12px;border:1px solid #fde68a;">
            <div style="font-weight:700;font-size:1rem;color:#92400e;margin-bottom:10px;">明日重点关注</div>
            <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:8px;">`;
        tomorrowFocus.forEach(f => {
            focusHtml += `<div style="padding:10px;background:#fff;border-radius:8px;border:1px solid #fde68a;">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px;">
                    <span style="font-weight:600;color:#1e293b;font-size:0.9rem;">${escapeHtml(f.name)}</span>
                    ${f.score > 0 ? `<span style="font-size:0.8rem;color:#dc2626;font-weight:600;background:#fef2f2;padding:2px 8px;border-radius:4px;">${f.score}分</span>` : ''}
                </div>
                <div style="font-size:0.75rem;color:#78716c;">${escapeHtml(f.reason)}</div>
            </div>`;
        });
        focusHtml += '</div></div>';
    }
    
    return `
        <div class="review-card ${isLatest ? 'latest' : ''}" style="border-radius:16px;">
            <div class="review-header" style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px;">
                <div class="review-date" style="font-size:1.2rem;font-weight:700;color:#1e293b;">${date} 收评</div>
                <div class="review-time" style="font-size:0.8rem;color:#94a3b8;">${new Date(review.timestamp).toLocaleTimeString()}</div>
            </div>
            
            <!-- 摘要 -->
            <div style="padding:12px 16px;background:#f8fafc;border-radius:10px;margin-bottom:16px;font-size:0.9rem;line-height:1.8;color:#334155;white-space:pre-line;">${escapeHtml(review.summary)}</div>
            
            <!-- 大盘走势 -->
            <div style="margin-bottom:20px;">
                <div style="font-weight:600;font-size:0.95rem;color:#1e293b;margin-bottom:10px;">大盘走势</div>
                <div style="display:flex;gap:12px;flex-wrap:wrap;">${marketHtml || '<div style="color:#94a3b8;">暂无数据</div>'}</div>
            </div>
            
            <!-- 行业板块 -->
            ${renderSectorList(industrySectors, '行业板块')}
            
            <!-- 概念板块 -->
            ${renderSectorList(conceptSectors, '概念板块')}
            
            <!-- 涨停股 -->
            <div style="margin-bottom:16px;">
                ${renderStockList(limitUp, '涨停个股', true)}
            </div>
            
            <!-- 跌停股 -->
            <div style="margin-bottom:16px;">
                ${renderStockList(limitDown, '跌停个股', false)}
            </div>
            
            <!-- 涨幅排行 -->
            ${renderRankList(topGainers, '涨幅排行', true)}
            
            <!-- 跌幅排行 -->
            ${renderRankList(topLosers, '跌幅排行', false)}
            
            <!-- 明日关注 -->
            ${focusHtml}
        </div>
    `;
}

// 页面初始化
document.addEventListener('DOMContentLoaded', async function() {
    // 0. 加载大盘指数
    loadIndexBar();
    setInterval(loadIndexBar, 10000);
    
    // 1. 从服务器加载股票映射表
    await loadStockMapFromServer();
    
    // 2. 加载自定义扫描池
    await loadScanPoolFromAPI();

    // 2.5 加载股票标签
    await loadStockTagsFromAPI();

    // 3. 初始化快捷按钮
    await loadHotStocksFromAPI();
    await initQuickButtons();
    
    // 4. 检查并自动更新趋势数据
    await checkAndAutoUpdateTrend();
    
    // 绑定查询按钮事件
    fetchBtn.addEventListener('click', onFetchOrRefresh);
    stockInput.addEventListener('keypress', function(e) {
        if (e.key === 'Enter') onFetchOrRefresh();
    });
    
    // 绑定手动刷新按钮
    refreshNowBtn.addEventListener('click', manualRefresh);
    
    // 更新交易状态显示
    const updateTradingStatus = () => {
        const statusEl = document.getElementById('tradingStatus');
        if (statusEl) {
            statusEl.textContent = getTradingStatusText();
        }
        const scanStatusEl = document.getElementById('scanTradingStatus');
        if (scanStatusEl) {
            scanStatusEl.textContent = isTradingTime() ? '📊 交易时间，自动5秒刷新' : '💤 非交易时间';
        }
    };
    updateTradingStatus();
    setInterval(updateTradingStatus, 10000); // 每10秒更新一次状态文本
    
    // 启动智能刷新
    scheduleRefresh();
    
    // 股票筛选不自动启动扫描，等切换到该tab时再启动
    // 但仍需停止之前的自动扫描（初始不在该tab）
    stopAutoScan();
    
    // 绑定智能推荐按钮
    const genRecommendBtn = document.getElementById('genRecommendBtn');
    const saveRecommendBtn = document.getElementById('saveRecommendBtn');
    
    if (genRecommendBtn) {
        genRecommendBtn.addEventListener('click', generateRecommendations);
    }
    
    if (saveRecommendBtn) {
        saveRecommendBtn.addEventListener('click', saveRecommendations);
    }
    
    // 绑定添加股票按钮
    const addStockBtn = document.getElementById('addStockBtn');
    const addStockInput = document.getElementById('addStockInput');
    
    if (addStockBtn) {
        addStockBtn.addEventListener('click', function() {
            if (!addStockInput) return;
            const symbol = addStockInput.value.trim();
            addToScanPool(symbol);
            addStockInput.value = '';
        });
    }
    
    if (addStockInput) {
        addStockInput.addEventListener('keypress', function(e) {
            if (e.key === 'Enter') {
                const symbol = addStockInput.value.trim();
                addToScanPool(symbol);
                addStockInput.value = '';
            }
        });
    }

    // 绑定刷新热门股票按钮
    const refreshHotStocksBtn = document.getElementById('refreshHotStocksBtn');
    if (refreshHotStocksBtn) {
        refreshHotStocksBtn.addEventListener('click', async function() {
            hotStocksCache = [];
            await initQuickButtons();
        });
    }

    // 绑定K线弹窗关闭
    const closeKlineBtn = document.getElementById('closeKlineModal');
    const klineModal = document.getElementById('klineModal');
    if (closeKlineBtn) {
        closeKlineBtn.addEventListener('click', closeKlineModal);
    }
    if (klineModal) {
        klineModal.addEventListener('click', function(e) {
            if (e.target === klineModal) {
                closeKlineModal();
            }
        });
    }
    
    // 初始化Tab
    initTabs();
    
    // 加载热门股票
    await initQuickButtons();
    
    // 加载历史推荐记录
    await renderHistoryRecommendations();
    
    // 渲染股票池
    renderScanPool();
    
    // 启动大盘监控
    startMarketMonitoring();
    
    // 默认加载第一只股票
    const quickStocks = hotStocksCache.length > 0 ? hotStocksCache : getQuickStocks();
    if (quickStocks.length > 0) {
        currentSymbol = quickStocks[0].code;
        stockInput.value = currentSymbol;
        fetchStockQuote(currentSymbol, false);
    }
    
    
    // 绑定策略优化按钮
    const refreshWeightsBtn = document.getElementById('refreshWeightsBtn');
    if (refreshWeightsBtn) refreshWeightsBtn.addEventListener('click', loadStrategyWeights);
    const runBacktestBtn = document.getElementById('runBacktestBtn');
    if (runBacktestBtn) runBacktestBtn.addEventListener('click', runStrategyBacktest);
    const resetWeightsBtn = document.getElementById('resetWeightsBtn');
    if (resetWeightsBtn) resetWeightsBtn.addEventListener('click', resetStrategyWeights);
    
    // 加载收评历史
    await loadDailyReviews();
    
    // 加载策略数据
    loadStrategyWeights();
    loadBacktestHistory();
    loadPredictionRecords();
});

// ========== 股票标签相关功能 ==========
let stockTagsCache = {};  // 股票标签缓存

// 从API加载所有股票标签
async function loadStockTagsFromAPI() {
    try {
        const response = await fetch('/api/stock-tags');
        const result = await response.json();
        if (result.success && result.data && result.data.tags) {
            stockTagsCache = result.data.tags;
            // 同时更新window.stockTags
            window.stockTags = result.data.tags;
            return result.data.tags;
        }
    } catch (error) {
        console.error('加载股票标签失败:', error);
    }
    return window.stockTags || {};  // 回退到原始配置
}

// 获取某只股票的标签
async function getStockTagsFromAPI(symbol) {
    try {
        const response = await fetch(`/api/stock-tags/${symbol}`);
        const result = await response.json();
        if (result.success && result.data) {
            const tags = result.data.tags;
            // 更新缓存
            stockTagsCache[symbol.toLowerCase()] = tags;
            return tags;
        }
    } catch (error) {
        console.error('获取股票标签失败:', error);
    }
    return stockTagsCache[symbol.toLowerCase()] || [];
}

// 添加标签到股票
async function addTagsToStockAPI(symbol, tags) {
    try {
        const response = await fetch(`/api/stock-tags/${symbol}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags: tags })
        });
        const result = await response.json();
        if (result.success && result.data) {
            // 更新缓存
            stockTagsCache[symbol.toLowerCase()] = result.data.tags;
            return result.data;
        }
    } catch (error) {
        console.error('添加股票标签失败:', error);
    }
    return { success: false };
}

// 删除股票标签
async function removeTagFromStockAPI(symbol, tag = null) {
    try {
        const options = {
            method: 'DELETE',
            headers: { 'Content-Type': 'application/json' }
        };
        if (tag) {
            options.body = JSON.stringify({ tag: tag });
        }
        const response = await fetch(`/api/stock-tags/${symbol}`, options);
        const result = await response.json();
        if (result.success && result.data) {
            // 更新缓存
            if (result.data.tags.length === 0) {
                delete stockTagsCache[symbol.toLowerCase()];
            } else {
                stockTagsCache[symbol.toLowerCase()] = result.data.tags;
            }
            return result.data;
        }
    } catch (error) {
        console.error('删除股票标签失败:', error);
    }
    return { success: false };
}

// 获取股票概念建议
async function fetchStockConceptsAPI(symbol, name = null) {
    try {
        let url = `/api/stock-concepts/${symbol}`;
        if (name) {
            url += `?name=${encodeURIComponent(name)}`;
        }
        const response = await fetch(url);
        const result = await response.json();
        if (result.success && result.data) {
            return result.data;
        }
    } catch (error) {
        console.error('获取股票概念失败:', error);
    }
    return null;
}

// 渲染标签（带删除按钮）
function renderTags(tags, symbol, isEditable = true) {
    if (!tags || tags.length === 0) {
        return '<span class="tag-empty">暂无标签</span>';
    }
    
    return tags.map(tag => {
        if (isEditable) {
            return `
                <span class="tag-item" data-tag="${escapeHtml(tag)}">
                    ${escapeHtml(tag)}
                    <button class="tag-remove-btn" onclick="handleRemoveTag('${symbol}', '${escapeHtml(tag)}')">&times;</button>
                </span>
            `;
        } else {
            return `<span class="tag-item">${escapeHtml(tag)}</span>`;
        }
    }).join('');
}

// 处理删除标签
async function handleRemoveTag(symbol, tag) {
    if (!confirm(`确定要删除标签"${tag}"吗？`)) return;
    
    const result = await removeTagFromStockAPI(symbol, tag);
    if (result.success) {
        renderScanPool();  // 重新渲染股票池
    }
}

// 处理获取股票概念
async function handleFetchConcepts(symbol, name) {
    const result = await fetchStockConceptsAPI(symbol, name);
    if (result && result.concepts) {
        // 显示概念建议对话框
        showConceptSuggestions(symbol, name, result);
    }
}

// 显示概念建议对话框
function showConceptSuggestions(symbol, name, result) {
    const modalId = 'conceptModal';
    
    // 检查是否已存在模态框
    let modal = document.getElementById(modalId);
    if (!modal) {
        // 创建模态框
        modal = document.createElement('div');
        modal.id = modalId;
        modal.className = 'modal';
        document.body.appendChild(modal);
    }
    
    const existingTags = result.existing_tags || [];
    const suggestions = result.suggestions || [];
    
    // 过滤掉已存在的标签
    const newSuggestions = suggestions.filter(tag => !existingTags.includes(tag));
    
    modal.innerHTML = `
        <div class="modal-content concept-modal">
            <div class="modal-header">
                <h3>📊 ${escapeHtml(name)} (${symbol.toUpperCase()}) - 概念标签</h3>
                <button class="modal-close" onclick="closeConceptModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="concept-section">
                    <h4>🏷️ 已有标签</h4>
                    <div class="tags-container">
                        ${existingTags.length > 0 
                            ? existingTags.map(tag => `<span class="tag-item">${escapeHtml(tag)}</span>`).join('')
                            : '<span style="color:#94a3b8;">暂无标签</span>'}
                    </div>
                </div>
                ${newSuggestions.length > 0 ? `
                <div class="concept-section">
                    <h4>💡 建议添加的概念</h4>
                    <div class="tags-container suggestions">
                        ${newSuggestions.map(tag => `
                            <span class="tag-item suggestion" data-tag="${escapeHtml(tag)}"
                                  onclick="toggleSuggestionTag(this, '${escapeHtml(tag)}')">
                                ${escapeHtml(tag)}
                            </span>
                        `).join('')}
                    </div>
                </div>
                ` : ''}
                <div class="concept-section">
                    <h4>✏️ 自定义标签</h4>
                    <div class="input-group">
                        <input type="text" id="customTagInput" placeholder="输入自定义标签，多个用逗号分隔"
                               style="flex:1; padding:0.5rem; border:1px solid #cbd5e1; border-radius:0.375rem;">
                        <button class="btn-secondary" onclick="addCustomTags('${symbol}')">添加</button>
                    </div>
                </div>
            </div>
            <div class="modal-footer">
                ${newSuggestions.length > 0 ? `
                <button class="btn-scan" onclick="addSelectedSuggestions('${symbol}')">
                    ✅ 添加选中的建议标签
                </button>
                ` : ''}
                <button class="btn-secondary" onclick="closeConceptModal()">关闭</button>
            </div>
        </div>
    `;
    
    modal.style.display = 'flex';
}

// 关闭概念建议对话框
function closeConceptModal() {
    const modal = document.getElementById('conceptModal');
    if (modal) {
        modal.style.display = 'none';
    }
}

// 切换建议标签的选中状态
function toggleSuggestionTag(element, tag) {
    element.classList.toggle('selected');
}

// 添加选中的建议标签
async function addSelectedSuggestions(symbol) {
    const selectedTags = [];
    const modal = document.getElementById('conceptModal');
    if (modal) {
        const selectedElements = modal.querySelectorAll('.tag-item.suggestion.selected');
        selectedElements.forEach(el => {
            const tag = el.getAttribute('data-tag');
            if (tag) selectedTags.push(tag);
        });
    }
    
    if (selectedTags.length === 0) {
        alert('请先选择要添加的标签');
        return;
    }
    
    const result = await addTagsToStockAPI(symbol, selectedTags);
    if (result.success) {
        alert(`成功添加 ${selectedTags.length} 个标签`);
        closeConceptModal();
        renderScanPool();  // 重新渲染股票池
    }
}

// 添加自定义标签
async function addCustomTags(symbol) {
    const input = document.getElementById('customTagInput');
    if (!input) return;
    
    const value = input.value.trim();
    if (!value) {
        alert('请输入标签内容');
        return;
    }
    
    // 分割标签（支持逗号和空格分隔）
    const tags = value.split(/[,，\s]+/).filter(tag => tag.trim());
    
    if (tags.length === 0) {
        alert('请输入有效的标签');
        return;
    }
    
    const result = await addTagsToStockAPI(symbol, tags);
    if (result.success) {
        alert(`成功添加 ${tags.length} 个标签`);
        input.value = '';
        closeConceptModal();
        renderScanPool();  // 重新渲染股票池
    }
}

// ========== 大盘预测预警系统（主力资金流向预测） ==========
let lastPredictStatus = null;
let predictRefreshInterval = null;

// 获取大盘预测数据（基于主力资金流向）
async function checkMarketPredict() {
    try {
        const response = await fetch('/api/market-predict');
        const result = await response.json();
        if (result.success && result.data) {
            renderMarketPredict(result.data);
            return result.data;
        }
    } catch (error) {
        console.error('[大盘预测] 获取预测数据失败:', error);
    }
    return null;
}

// 渲染大盘预测预警
function renderMarketPredict(data) {
    const container = document.getElementById('market-alert-container');
    if (!container) return;

    const { predictStatus, riskScore, prediction, signals, details, updateTime } = data;
    
    // 如果安全且之前也是安全，不显示
    if (predictStatus === 'safe' && lastPredictStatus === 'safe') {
        container.innerHTML = '';
        return;
    }
    
    lastPredictStatus = predictStatus;

    const iconMap = {
        'info': 'ℹ️',
        'warning': '👀',
        'danger': '⚠️',
        'severe_danger': '🚨'
    };

    const classMap = {
        'info': 'market-alert-info',
        'warning': 'market-alert-warning',
        'danger': 'market-alert-danger',
        'severe_danger': 'market-alert-danger'
    };

    const alertClass = classMap[predictStatus] || 'market-alert-info';
    const icon = iconMap[predictStatus] || 'ℹ️';

    // 风险仪表盘
    const riskBarColor = riskScore >= 70 ? '#e74c3c' : riskScore >= 50 ? '#e67e22' : riskScore >= 30 ? '#f39c12' : '#27ae60';
    const riskBarWidth = Math.min(riskScore, 100);

    // 信号指标列表
    let signalsHtml = '';
    if (signals && Object.keys(signals).length > 0) {
        signalsHtml = '<div class="market-predict-signals">';
        for (const [key, signal] of Object.entries(signals)) {
            const signalIcon = signal.status === 'danger' ? '🔴' : signal.status === 'warning' ? '🟡' : '🟢';
            const signalWidth = (signal.score / signal.maxScore) * 100;
            signalsHtml += `
                <div class="predict-signal-item">
                    <div class="predict-signal-header">
                        <span class="predict-signal-name">${signalIcon} ${escapeHtml(signal.name)}</span>
                        <span class="predict-signal-value">${escapeHtml(signal.value)}</span>
                    </div>
                    <div class="predict-signal-bar">
                        <div class="predict-signal-fill ${signal.status}" style="width: ${signalWidth}%"></div>
                    </div>
                    <div class="predict-signal-detail">${escapeHtml(signal.detail)}</div>
                </div>
            `;
        }
        signalsHtml += '</div>';
    }

    // 详细数据
    let detailsHtml = '';
    if (details && details.totalStocks) {
        // 计算百分比，避免除零
        const upRatio = details.totalStocks > 0 ? (details.upCount / details.totalStocks * 100).toFixed(1) : '0.0';
        const downRatio = details.totalStocks > 0 ? (details.downCount / details.totalStocks * 100).toFixed(1) : '0.0';
        const volumeDumpRatio = details.totalStocks > 0 ? (details.volumeDumpCount / details.totalStocks * 100).toFixed(1) : '0.0';
        
        detailsHtml = `
            <div class="market-predict-details">
                <div class="predict-detail-row">
                    <span>📊 扫描股票</span>
                    <span>${details.totalStocks} 只</span>
                </div>
                <div class="predict-detail-row">
                    <span>📈 上涨</span>
                    <span style="color: #e74c3c">${details.upCount} 只 (${upRatio}%)</span>
                </div>
                <div class="predict-detail-row">
                    <span>📉 下跌</span>
                    <span style="color: #27ae60">${details.downCount} 只 (${downRatio}%)</span>
                </div>
                <div class="predict-detail-row">
                    <span>💥 大跌(>3%)</span>
                    <span style="color: #27ae60; font-weight: 700">${details.heavyDownCount} 只</span>
                </div>
                <div class="predict-detail-row">
                    <span>📉 放量下跌</span>
                    <span style="color: ${details.volumeDumpCount > 20 ? '#e74c3c' : '#f39c12'}">${details.volumeDumpCount} 只 (${volumeDumpRatio}%)</span>
                </div>
                <div class="predict-detail-row">
                    <span>💹 平均委比</span>
                    <span style="color: ${details.avgWeibi < -20 ? '#e74c3c' : details.avgWeibi < 0 ? '#f39c12' : '#27ae60'}">${details.avgWeibi}</span>
                </div>
            </div>
        `;
    }

    const updateTimeStr = new Date(updateTime).toLocaleTimeString('zh-CN', { hour12: false });

    container.innerHTML = `
        <div class="market-alert ${alertClass}">
            <div class="market-alert-header">
                <span class="market-alert-icon">${icon}</span>
                <h3 class="market-alert-title">${escapeHtml(prediction.title)}</h3>
                <span class="market-risk-badge" style="background: ${riskBarColor}">风险 ${riskScore}/100</span>
            </div>
            
            <!-- 风险等级条 -->
            <div class="market-risk-bar-container">
                <div class="market-risk-bar">
                    <div class="market-risk-fill" style="width: ${riskBarWidth}%; background: ${riskBarColor}"></div>
                    <div class="market-risk-labels">
                        <span class="${riskScore >= 60 ? 'active' : ''}" style="left: 60%">危险</span>
                        <span class="${riskScore >= 40 && riskScore < 60 ? 'active' : ''}" style="left: 40%">警惕</span>
                        <span class="${riskScore < 40 ? 'active' : ''}" style="left: 20%">安全</span>
                    </div>
                </div>
            </div>
            
            <p class="market-alert-message">${escapeHtml(prediction.message)}</p>
            
            <!-- 操作按钮 -->
            <div class="market-alert-actions">
                <button class="market-alert-action ${predictStatus}" onclick="handleMarketAction('${prediction.shouldSell}')">
                    ${prediction.shouldSell ? '💸 ' : '👀 '}${escapeHtml(prediction.action)}
                </button>
            </div>
            
            <!-- 详细信号 -->
            <div class="market-predict-section">
                <div class="market-predict-section-title" onclick="togglePredictDetail(this)">
                    📋 查看详细信号分析
                    <span class="collapse-arrow">▼</span>
                </div>
                <div class="market-predict-section-content">
                    ${signalsHtml}
                    ${detailsHtml}
                </div>
            </div>
            
            <div class="market-alert-footer">
                <span>🕐 ${updateTimeStr} · 每30秒自动刷新</span>
                <button class="market-alert-refresh" onclick="checkMarketPredict()">🔄 刷新</button>
            </div>
        </div>
    `;

    // 严重风险播放警报音效
    if (predictStatus === 'severe_danger') {
        playAlertSound();
    }
}

// 切换详细信号显示
function togglePredictDetail(header) {
    const content = header.nextElementSibling;
    const arrow = header.querySelector('.collapse-arrow');
    if (content) {
        const isHidden = content.style.display === 'none' || !content.style.display;
        content.style.display = isHidden ? 'block' : 'none';
        if (arrow) {
            arrow.style.transform = isHidden ? 'rotate(180deg)' : 'rotate(0deg)';
        }
    }
}

// 处理操作建议
function handleMarketAction(shouldSell) {
    if (shouldSell === 'True' || shouldSell === true) {
        if (confirm('⚠️ 根据主力资金流向预测，建议您考虑清仓或减仓以避免亏损。\n\n是否跳转到股票池查看持仓？')) {
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.querySelector('[data-tab=tab-quote]').classList.add('active');
            window.scrollTo({ top: 0, behavior: 'smooth' });
        }
    }
}

// 播放警报音效
function playAlertSound() {
    try {
        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const oscillator = audioContext.createOscillator();
        const gainNode = audioContext.createGain();
        
        oscillator.connect(gainNode);
        gainNode.connect(audioContext.destination);
        
        oscillator.frequency.value = 800;
        oscillator.type = 'sine';
        gainNode.gain.value = 0.3;
        
        oscillator.start();
        setTimeout(() => {
            oscillator.stop();
            audioContext.close();
        }, 500);
    } catch (e) {
        console.log('[大盘] 无法播放警报音效');
    }
}

// 判断交易时间
// 盘中：交易日 09:30-11:30 和 13:00-15:00
// 盘后：交易日 15:00 之后，以及所有非交易日（周末、节假日）
// 节假日：CN_HOLIDAYS 列表（可按需手动维护元旦/春节/清明/劳动节/端午/中秋/国庆等）
const CN_HOLIDAYS = [
    // '2026-01-01', // 元旦
    // '2026-02-17', // 春节
    // '2026-04-04', // 清明节
    // '2026-05-01', // 劳动节
    // '2026-06-19', // 端午节（按农历，每年单独维护）
    // '2026-09-25', // 中秋节
    // '2026-10-01', // 国庆节
    // '2026-10-02',
    // '2026-10-03',
    // '2026-10-04',
    // '2026-10-05',
    // '2026-10-06',
    // '2026-10-07',
    // '2026-10-08',
];

function isHoliday(now = new Date()) {
    const dateStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
    return CN_HOLIDAYS.includes(dateStr);
}

function isTradingTime() {
    const now = new Date();
    const hour = now.getHours();
    const minute = now.getMinutes();
    const day = now.getDay();

    // 周末不交易
    if (day === 0 || day === 6) return false;
    // 法定节假日不交易
    if (isHoliday(now)) return false;

    const timeNum = hour * 100 + minute;
    return (timeNum >= 930 && timeNum <= 1130) || (timeNum >= 1300 && timeNum <= 1500);
}

// 启动大盘监控
function startMarketMonitoring() {
    checkMarketPredict();
    
    if (predictRefreshInterval) {
        clearInterval(predictRefreshInterval);
    }
    predictRefreshInterval = setInterval(checkMarketPredict, 30000);
}

// ========== 策略优化功能 ==========

async function loadStrategyWeights() {
    const container = document.getElementById('strategyWeights');
    if (!container) return;
    try {
        const response = await fetch('/api/strategy/weights');
        const result = await response.json();
        if (!result.success) throw new Error('获取失败');
        
        const data = result.data;
        let html = '';
        for (const [factor, info] of Object.entries(data)) {
            const weightPercent = Math.round(info.weight * 100);
            const accuracyPercent = info.accuracy;
            const weightColor = info.weight > 1.2 ? '#dc2626' : info.weight < 0.8 ? '#2563eb' : '#16a34a';
            const accColor = accuracyPercent > 60 ? '#16a34a' : accuracyPercent < 40 ? '#dc2626' : '#f59e0b';
            const barWidth = Math.min(100, weightPercent);
            
            html += `
            <div class="strategy-card">
                <div class="strategy-card-header">
                    <span class="strategy-card-title">${escapeHtml(info.description)}</span>
                    <span class="strategy-card-subtitle">${factor}</span>
                </div>
                <div class="strategy-bar-row">
                    <span class="strategy-bar-label">权重</span>
                    <div class="strategy-bar-track">
                        <div class="strategy-bar-fill" style="width:${barWidth}%;background:${weightColor};"></div>
                    </div>
                    <span class="strategy-bar-value" style="color:${weightColor};">${info.weight.toFixed(2)}</span>
                </div>
                <div class="strategy-bar-row">
                    <span class="strategy-bar-label">准确率</span>
                    <div class="strategy-bar-track">
                        <div class="strategy-bar-fill" style="width:${accuracyPercent}%;background:${accColor};"></div>
                    </div>
                    <span class="strategy-bar-value" style="color:${accColor};">${accuracyPercent}%</span>
                </div>
                <div class="strategy-card-footer">样本: ${info.sample_count} | 正确: ${info.correct_count}</div>
            </div>`;
        }
        container.innerHTML = html;
    } catch (e) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--accent-danger);">加载失败: ' + escapeHtml(e.message) + '</div>';
    }
}

async function loadBacktestHistory() {
    const container = document.getElementById('backtestHistory');
    if (!container) return;
    try {
        const response = await fetch('/api/strategy/backtest-history?days=30');
        const result = await response.json();
        if (!result.success) throw new Error('获取失败');
        
        backtestData = result.data || [];
        backtestPage = 1;
        renderBacktestHistory();
    } catch (e) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--accent-danger);">加载失败: ' + escapeHtml(e.message) + '</div>';
    }
}

function renderBacktestHistory() {
    const container = document.getElementById('backtestHistory');
    if (!container) return;
    
    if (backtestData.length === 0) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-tertiary);">暂无回测数据，每日15:20自动回测，或点击"手动回测"</div>';
        return;
    }
    
    const totalPages = Math.ceil(backtestData.length / BACKTEST_PAGE_SIZE);
    const start = (backtestPage - 1) * BACKTEST_PAGE_SIZE;
    const pageData = backtestData.slice(start, start + BACKTEST_PAGE_SIZE);
    
    let html = '';
    pageData.forEach(report => {
        const accColor = report.accuracy > 60 ? 'var(--accent-success)' : report.accuracy < 40 ? 'var(--accent-danger)' : 'var(--accent-warning)';
        const marketDir = report.market_change > 0 ? '📈 上涨' : report.market_change < 0 ? '📉 下跌' : '➡️ 平盘';
        const marketColor = report.market_change > 0 ? 'var(--accent-danger)' : report.market_change < 0 ? 'var(--accent-success)' : 'var(--text-secondary)';
        
        // 误判分析
        let analysisHtml = '';
        const analysis = report.misjudge_analysis;
        if (analysis && typeof analysis === 'object') {
            if (analysis.market_context) analysisHtml += `<div style="margin-bottom:4px;">📊 大盘: <span style="color:${marketColor}">${escapeHtml(analysis.market_context)}</span></div>`;
            if (analysis.bull_misjudge_analysis) analysisHtml += `<div style="margin-bottom:4px;color:var(--accent-danger);">🔴 误判分析: ${escapeHtml(analysis.bull_misjudge_analysis)}</div>`;
            if (analysis.suggestions && analysis.suggestions.length > 0) {
                analysisHtml += '<div style="margin-top:6px;"><strong>优化建议:</strong></div>';
                analysis.suggestions.forEach(s => { analysisHtml += `<div style="color:#7c3aed;">💡 ${escapeHtml(s)}</div>`; });
            }
        }
        
        // 权重调整
        let adjHtml = '';
        const adjustments = report.weight_adjustments;
        if (adjustments && Array.isArray(adjustments)) {
            const changed = adjustments.filter(a => a.action !== 'keep');
            if (changed.length > 0) {
                adjHtml = '<div style="margin-top:8px;"><strong>权重调整:</strong></div>';
                changed.forEach(a => {
                    const icon = a.action === 'increase' ? '⬆️' : '⬇️';
                    adjHtml += `<div style="font-size:0.8rem;">${icon} ${escapeHtml(a.factor)}: ${escapeHtml(a.reason)}</div>`;
                });
            }
        }
        
        html += `
        <div class="backtest-card">
            <div class="backtest-header">
                <span class="backtest-date">${report.backtest_date}</span>
                <span class="backtest-market" style="color:${marketColor};">${marketDir} ${report.market_change > 0 ? '+' : ''}${parseFloat(report.market_change).toFixed(2)}%</span>
            </div>
            <div class="backtest-grid">
                <div class="backtest-grid-item">
                    <div class="backtest-grid-label">总准确率</div>
                    <div class="backtest-grid-value" style="color:${accColor};">${report.accuracy}%</div>
                </div>
                <div class="backtest-grid-item">
                    <div class="backtest-grid-label">看涨准确率</div>
                    <div class="backtest-grid-value" style="color:var(--accent-danger);">${report.bull_accuracy}%</div>
                </div>
                <div class="backtest-grid-item">
                    <div class="backtest-grid-label">预测总数</div>
                    <div class="backtest-grid-value" style="color:var(--text-primary);">${report.total_predictions}</div>
                </div>
            </div>
            ${analysisHtml ? `<div class="backtest-analysis">${analysisHtml}</div>` : ''}
            ${adjHtml ? `<div class="backtest-adjustments">${adjHtml}</div>` : ''}
        </div>`;
    });
    
    // 翻页按钮
    if (totalPages > 1) {
        html += `
        <div style="display:flex;justify-content:center;align-items:center;gap:12px;margin-top:16px;">
            <button class="btn-secondary" onclick="backtestPrevPage()" ${backtestPage === 1 ? 'disabled' : ''} style="padding:6px 12px; ${backtestPage === 1 ? 'opacity:0.5;cursor:not-allowed;' : ''}">⬅️ 上一页</button>
            <span style="color:var(--text-secondary);font-size:0.9rem;">第 ${backtestPage} / ${totalPages} 页</span>
            <button class="btn-secondary" onclick="backtestNextPage()" ${backtestPage === totalPages ? 'disabled' : ''} style="padding:6px 12px; ${backtestPage === totalPages ? 'opacity:0.5;cursor:not-allowed;' : ''}">下一页 ➡️</button>
        </div>`;
    }
    
    container.innerHTML = html;
}

function backtestPrevPage() {
    if (backtestPage > 1) {
        backtestPage--;
        renderBacktestHistory();
    }
}

function backtestNextPage() {
    const totalPages = Math.ceil(backtestData.length / BACKTEST_PAGE_SIZE);
    if (backtestPage < totalPages) {
        backtestPage++;
        renderBacktestHistory();
    }
}

let predictionData = [];
let predictionPage = 1;
const PREDICTION_PAGE_SIZE = 5;

let backtestData = [];
let backtestPage = 1;
const BACKTEST_PAGE_SIZE = 3;

async function loadPredictionRecords() {
    const container = document.getElementById('predictionRecords');
    if (!container) return;
    try {
        const response = await fetch('/api/strategy/predictions?include_today_and_pending=true');
        const result = await response.json();
        
        if (!result.success) throw new Error('获取失败');
        
        predictionData = result.data || [];
        predictionPage = 1;
        renderPredictionRecords();
    } catch (e) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--accent-danger);">加载失败: ' + escapeHtml(e.message) + '</div>';
    }
}

function renderPredictionRecords() {
    const container = document.getElementById('predictionRecords');
    if (!container) return;
    
    if (predictionData.length === 0) {
        container.innerHTML = '<div style="text-align:center;padding:2rem;color:var(--text-tertiary);">暂无预测记录，扫描股票后自动生成</div>';
        return;
    }
    
    const totalPages = Math.ceil(predictionData.length / PREDICTION_PAGE_SIZE);
    const start = (predictionPage - 1) * PREDICTION_PAGE_SIZE;
    const pageData = predictionData.slice(start, start + PREDICTION_PAGE_SIZE);
    
    let html = '<div class="stock-table"><div class="stock-table-header">';
    html += '<span>日期</span><span>名称</span><span>评分</span><span>涨跌幅</span><span>外盘占比</span><span>量比</span><span>委比</span><span>验证</span>';
    html += '</div>';
    
    pageData.forEach(r => {
        const verifiedText = r.verified ? (r.is_correct ? '✅正确' : '❌错误') : '⏳待验证';
        const verifiedColor = r.verified ? (r.is_correct ? 'var(--accent-success)' : 'var(--accent-danger)') : 'var(--accent-tertiary)';
        const dateShort = r.predict_date ? r.predict_date.slice(5) : '';
        
        html += `
        <div class="stock-table-row">
            <span style="font-size:0.75rem;color:var(--text-secondary);font-weight:500;">${dateShort}</span>
            <span class="name">${escapeHtml(r.name)}<br><small style="color:var(--text-tertiary);">${r.symbol.toUpperCase()}</small></span>
            <span style="font-weight:600;color:${r.score >= 60 ? 'var(--accent-danger)' : r.score >= 40 ? 'var(--accent-warning)' : 'var(--accent-success)'};">${r.score}</span>
            <span style="color:${parseFloat(r.change_percent) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)'};">${parseFloat(r.change_percent) >= 0 ? '+' : ''}${parseFloat(r.change_percent).toFixed(2)}%</span>
            <span>${parseFloat(r.outer_ratio).toFixed(1)}%</span>
            <span>${parseFloat(r.volume_ratio).toFixed(2)}</span>
            <span style="color:${parseFloat(r.weibi) >= 0 ? 'var(--accent-danger)' : 'var(--accent-success)'};">${parseFloat(r.weibi) >= 0 ? '+' : ''}${parseFloat(r.weibi).toFixed(2)}%</span>
            <span style="color:${verifiedColor};font-weight:600;">${verifiedText}</span>
        </div>`;
    });
    html += '</div>';
    
    if (totalPages > 1) {
        html += `<div class="history-pagination">
            <button class="page-btn" onclick="predictionPage=Math.max(1,predictionPage-1);renderPredictionRecords();" ${predictionPage <= 1 ? 'disabled' : ''}>上一页</button>
            <span class="page-info">第 ${predictionPage} / ${totalPages} 页</span>
            <button class="page-btn" onclick="predictionPage=Math.min(${totalPages},predictionPage+1);renderPredictionRecords();" ${predictionPage >= totalPages ? 'disabled' : ''}>下一页</button>
        </div>`;
    }
    
    container.innerHTML = html;
}

async function runStrategyBacktest() {
    const btn = document.getElementById('runBacktestBtn');
    if (!btn) return;
    btn.disabled = true;
    btn.textContent = '🔬 回测中...';
    try {
        const response = await fetch('/api/strategy/backtest', { method: 'POST' });
        const result = await response.json();
        if (result.success) {
            const data = result.data;
            if (data.message) {
                alert(data.message);
            } else {
                alert(`回测完成!\n日期: ${data.date}\n大盘: ${data.market_change > 0 ? '上涨' : '下跌'} ${data.market_change.toFixed(2)}%\n准确率: ${data.accuracy}%\n看涨准确率: ${data.bull_accuracy}%`);
            }
        } else {
            alert('回测失败: ' + (result.detail || '未知错误'));
        }
    } catch (e) {
        alert('回测请求失败: ' + e.message);
    } finally {
        btn.disabled = false;
        btn.textContent = '🔬 手动回测';
        loadStrategyWeights();
        loadBacktestHistory();
        loadPredictionRecords();
    }
}

async function resetStrategyWeights() {
    if (!confirm('确定要重置所有策略权重为默认值吗？')) return;
    try {
        const response = await fetch('/api/strategy/weights/reset', { method: 'POST' });
        const result = await response.json();
        if (result.success) {
            alert('策略权重已重置为默认值');
            loadStrategyWeights();
        } else {
            alert('重置失败');
        }
    } catch (e) {
        alert('重置请求失败: ' + e.message);
    }
}
