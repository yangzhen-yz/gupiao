// main.css 死规则扫描 v3
// 死规则 = class 选择器名在整个 src + index.html 中既无字面引用、也无法被任何动态拼接源生成
// 动态拼接源提取：模板字符串 `top-${x}` / 普通字符串 'top-' / "timing-" 等以 - 结尾的字符串
const fs = require('fs')
const path = require('path')

const ROOT = path.join(__dirname, '..')   // 从脚本位置推，别写死本机绝对路径
const css = fs.readFileSync(path.join(ROOT, 'src/assets/main.css'), 'utf8')

// 1. 收集所有 class 选择器及行号
const classUse = new Map()
css.split('\n').forEach((line, i) => {
  const re = /\.(-?[A-Za-z_][A-Za-z0-9_-]*)/g
  let m
  while ((m = re.exec(line)) !== null) {
    const name = m[1]
    if (!classUse.has(name)) classUse.set(name, [])
    classUse.get(name).push(i + 1)
  }
})

// 2. 使用方语料
function walk(dir, out = []) {
  for (const f of fs.readdirSync(dir, { withFileTypes: true })) {
    const p = path.join(dir, f.name)
    if (f.isDirectory()) walk(p, out)
    else if (/\.(vue|js|html)$/.test(f.name)) out.push(p)
  }
  return out
}
const files = walk(path.join(ROOT, 'src'))
if (fs.existsSync(path.join(ROOT, 'index.html'))) files.push(path.join(ROOT, 'index.html'))
const corpus = files.map(f => fs.readFileSync(f, 'utf8')).join('\n')

// 3. 提取动态拼接源：所有以 - 结尾的字符串字面量 / 模板前缀
const concatPrefixes = new Set()
{
  const reTick = /`([A-Za-z_][A-Za-z0-9_-]*)-\$\{/g // `top-${x}`
  const reQuote = /['"]([A-Za-z_][A-Za-z0-9_-]*-)['"]/g // 'top-' / "top-"
  let m
  while ((m = reTick.exec(corpus)) !== null) concatPrefixes.add(m[1] + '-')
  while ((m = reQuote.exec(corpus)) !== null) concatPrefixes.add(m[1])
}

// 4. 判定
const dead = [],
  dyn = [],
  alive = []
for (const [name, lines] of classUse) {
  if (corpus.includes(name)) {
    alive.push(name)
    continue
  }
  // 能否被某个拼接源生成？
  let generated = false
  for (const p of concatPrefixes) {
    if (name.startsWith(p)) {
      generated = true
      break
    }
  }
  if (generated) {
    dyn.push([name, lines])
    continue
  }
  // 6 字符前缀兜底（保守）
  const prefix6 = name.slice(0, Math.min(6, name.length))
  if (corpus.includes(prefix6)) {
    dyn.push([name, lines])
    continue
  }
  dead.push([name, lines])
}

console.log('拼接源:', [...concatPrefixes].join(' '))
console.log(
  '总 class:',
  classUse.size,
  '| 存活:',
  alive.length,
  '| 动态生成:',
  dyn.length,
  '| 死规则候选:',
  dead.length
)
console.log('\n===== 死规则候选（类名 -> 行号）=====')
dead.sort((a, b) => a[1][0] - b[1][0])
for (const [name, lines] of dead)
  console.log(name.padEnd(30), lines.slice(0, 6).join(','), lines.length > 6 ? `(+${lines.length - 6})` : '')
