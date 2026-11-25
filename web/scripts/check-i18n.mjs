// Reports UI strings passed to t('...') that have no Russian/Georgian translation.
// Usage: npm run i18n:check   (exit code 1 when anything is missing)
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', 'src')
const localesDir = path.join(root, 'i18n', 'locales')

function walk(dir) {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
    const p = path.join(dir, e.name)
    return e.isDirectory() ? walk(p) : [p]
  })
}

const dict = { ru: new Set(), ka: new Set() }
for (const f of fs.readdirSync(localesDir).filter((n) => n.endsWith('.js'))) {
  const mod = await import(pathToFileURL(path.join(localesDir, f)).href)
  for (const lang of Object.keys(dict)) {
    for (const k of Object.keys(mod.default?.[lang] || {})) dict[lang].add(k)
  }
}

// t('text') / t("text") / t(`text`) with a literal first argument (no ${} interpolation).
const re = /\bt\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)"|`((?:[^`\\$]|\\.)*)`)/g
const unescape = (s) => s.replace(/\\(['"`\\])/g, '$1').replace(/\\n/g, '\n')

const used = new Map()
for (const file of walk(root).filter((p) => /\.(jsx?|mjs)$/.test(p) && !p.includes(`${path.sep}i18n${path.sep}`))) {
  const src = fs.readFileSync(file, 'utf8')
  for (const m of src.matchAll(re)) {
    const key = unescape(m[1] ?? m[2] ?? m[3])
    if (!used.has(key)) used.set(key, path.relative(root, file))
  }
}

let missing = 0
for (const lang of Object.keys(dict)) {
  const list = [...used].filter(([k]) => !dict[lang].has(k))
  if (list.length) {
    missing += list.length
    console.log(`\n[${lang}] ${list.length} missing:`)
    for (const [k, f] of list) console.log(`  ${f}: ${JSON.stringify(k)}`)
  }
}
console.log(`\n${used.size} distinct keys checked, ${missing} missing translations`)
process.exit(missing ? 1 : 0)
