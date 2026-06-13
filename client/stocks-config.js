// ========================================
// A股股票配置文件
// 仅配置热门股票快捷按钮
// 其他数据（名称映射、扫描池、标签）均从数据库动态加载
// ========================================

let quickStocks = [
    { name: '深科技', code: 'sz000021' },
    { name: '亨通光电', code: 'sh600487' },
    { name: '中天科技', code: 'sh600522' },
    { name: '东山精密', code: 'sz002384' },
    { name: '光迅科技', code: 'sz002281' },
    { name: '利通电子', code: 'sh603629' },
    { name: '通富微电', code: 'sz002156' },
    { name: '云南锗业', code: 'sz002428' },
    { name: '福晶科技', code: 'sz002222' },
    { name: '泰晶科技', code: 'sh603738' },
    { name: '杭电股份', code: 'sh603618' },
    { name: '恒润股份', code: 'sh603985' },
    { name: '长电科技', code: 'sh600584' },
    { name: '天赐材料', code: 'sz002709' },
    { name: '宏和科技', code: 'sh603256' },
    { name: '大族激光', code: 'sz002008' },
    { name: '兆易创新', code: 'sh603986' },
    { name: '东材科技', code: 'sh601208' },
    { name: '中材科技', code: 'sz002080' },
    { name: '天通股份', code: 'sh600330' },
    { name: '江海股份', code: 'sz002484' },
    { name: '鼎胜新材', code: 'sh603876' },
    { name: '中瓷电子', code: 'sz003031' },
    { name: '德明利', code: 'sz001309' },
    { name: '景旺电子', code: 'sh603228' },
    { name: '世运电路', code: 'sh603920' },
    { name: '中京电子', code: 'sz002579' },
    { name: '深南电路', code: 'sz002916' },
    { name: '鹏鼎控股', code: 'sz002938' },
    { name: '沪电股份', code: 'sz002463' },
    { name: '生益科技', code: 'sh600183' }
];

let stockNameMap = {};
let scanStockPool = [];
let stockTags = {};

if (typeof module !== 'undefined' && module.exports) {
    module.exports = { quickStocks, stockNameMap, scanStockPool, stockTags };
} else if (typeof window !== 'undefined') {
    window.quickStocks = quickStocks;
    window.stockNameMap = stockNameMap;
    window.scanStockPool = scanStockPool;
    window.stockTags = stockTags;
}
