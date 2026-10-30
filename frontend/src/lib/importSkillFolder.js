/**
 * 从本地文件夹读取技能文件（<input type="file" webkitdirectory> 的 FileList）。
 * 只收文本类文件（技能 = SKILL.md + 脚本/模板附件）；二进制与超大文件跳过。
 * 返回 { files: [{path, content}], skipped: number }，path 已去掉根文件夹名。
 */
export const TEXT_EXTS = new Set([
  '.md', '.txt', '.py', '.js', '.ts', '.jsx', '.tsx', '.json', '.yaml', '.yml',
  '.toml', '.ini', '.csv', '.sh', '.bash', '.sql', '.html', '.css', '.xml', '.env',
  '.jinja', '.j2', '.tpl', '.template', '.cfg', '.conf', '.gitignore', '.keep',
])
// 单文件上限 50MB：技能附件允许较大的 .py/数据文件；nginx client_max_body_size=100m 兜底
export const MAX_FILE_BYTES = 50 * 1024 * 1024
// 与后端 SkillFileIn.pattern 对齐：字母/数字/中文/._-，/ 分层
const PATH_OK = /^[A-Za-z0-9._一-龥-]+(\/[A-Za-z0-9._一-龥-]+)*$/

export async function readSkillFolder(fileList) {
  const files = []
  let skipped = 0
  for (const f of Array.from(fileList)) {
    // webkitRelativePath = "根文件夹/子路径/文件名"，去掉第一段
    const rel = (f.webkitRelativePath || f.name).split('/').slice(1).join('/')
    if (!rel || !PATH_OK.test(rel)) {
      skipped++
      continue
    }
    const ext = '.' + (rel.split('.').pop() || '').toLowerCase()
    if (!TEXT_EXTS.has(ext) || f.size > MAX_FILE_BYTES) {
      skipped++
      continue
    }
    files.push({ path: rel, content: await f.text() })
  }
  files.sort((a, b) => a.path.localeCompare(b.path))
  return { files, skipped }
}

/** 规范化技能名：小写字母/数字/连字符。 */
export function sanitizeSkillName(name) {
  return name
    .toLowerCase()
    .replace(/[^a-z0-9-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .replace(/-{2,}/g, '-') || 'imported-skill'
}
