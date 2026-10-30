// 文件预览公共组件（AGENT-OPS 从 ChatPage 抽取）：对话页工作区与运行中心只读预览共用。
// fetchRaw 收完整 URL——调用方决定走用户端点还是管理端点：
//   ChatPage:  /api/agents/{aid}/workspace/{fid}/raw
//   运行中心:  /api/admin/ops/workspace/{fid}/raw（后端写 admin.read_workspace 审计）
import { Download, ExternalLink, FileText, FileType2, Loader2, X, ZoomOut } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { getToken } from '@/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { cn } from '@/lib/utils'

// 拉原始字节（带鉴权）
export async function fetchRaw(url) {
  const resp = await fetch(url, {
    headers: { Authorization: `Bearer ${getToken()}` },
  })
  if (!resp.ok) throw new Error(`下载失败 (${resp.status})`)
  return resp.arrayBuffer()
}

// 触发浏览器保存（rawUrl 为完整 URL：用户端点/管理端点/共享端点均可）
export async function downloadRaw(rawUrl, filename) {
  const buf = await fetchRaw(rawUrl)
  const url = URL.createObjectURL(new Blob([buf]))
  const a = document.createElement('a')
  a.href = url
  a.download = (filename || 'file').split('/').pop()
  a.click()
  URL.revokeObjectURL(url)
}

function PreviewPanel({ rawUrl, file, onClose, onInsert, readOnly = false }) {
  const isExcel = /\.(xlsx|xls|csv)$/i.test(file.filename)
  // mime 优先、扩展名兜底：历史文件（如早期沙箱产物）mime 是 octet-stream
  const isImage = (file.mime || '').startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg)$/i.test(file.filename)
  const isHtml = /\.html?$/i.test(file.filename)
  const isPdf = (file.mime || '') === 'application/pdf' || /\.pdf$/i.test(file.filename)
  const isPptx = /\.pptx$/i.test(file.filename)
  // M25：docx 用 docx-preview 高保真渲染（接近 Word 阅读模式）；mime 优先、扩展名兜底
  const isDocx = (file.mime || '') === 'application/vnd.openxmlformats-officedocument.wordprocessingml.document' || /\.docx$/i.test(file.filename)
  const isLegacyDoc = /\.doc$/i.test(file.filename)
  const isMarkdown = /\.(md|markdown)$/i.test(file.filename)
  // rawUrl 固定以 /raw 结尾，导出 Word 端点同前缀（用户端点/管理端点/共享端点约定一致）
  const docxUrl = rawUrl.replace(/\/raw$/, '/export.docx')
  const docxName = file.filename.replace(/\.(md|markdown)$/i, '.docx')

  return (
    <div className="flex min-w-0 flex-1 flex-col border-r bg-background">
      <div className="flex shrink-0 items-center gap-2 border-b px-3 py-2">
        <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium" title={file.filename}>{file.filename}</span>
        {!readOnly && (
          <Button variant="outline" size="sm" className="h-7 shrink-0 text-xs" onClick={() => { onInsert(file.filename); onClose() }}>
            引用到输入框
          </Button>
        )}
        {isMarkdown && (
          <Button variant="outline" size="sm" className="h-7 shrink-0 gap-1 text-xs" title="把这份 Markdown 转成 Word 文档下载" onClick={() => downloadRaw(docxUrl, docxName)}>
            <FileType2 className="h-3.5 w-3.5" />
            导出 Word
          </Button>
        )}
        <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" title="下载" onClick={() => downloadRaw(rawUrl, file.filename)}>
          <Download className="h-4 w-4" />
        </Button>
        <Button variant="ghost" size="icon" className="h-7 w-7 shrink-0" title="关闭预览" onClick={onClose}>
          <X className="h-4 w-4" />
        </Button>
      </div>
      {isExcel ? (
        <ExcelPreview rawUrl={rawUrl} file={file} />
      ) : isImage ? (
        <RawImage rawUrl={rawUrl} file={file} />
      ) : isHtml ? (
        // HTML（员工做的网页版 PPT 等）直接渲染；sandbox=allow-scripts：脚本可跑但处于隔离源，碰不到本站存储与接口
        <HtmlPreview rawUrl={rawUrl} file={file} />
      ) : isPdf ? (
        <RawFrame rawUrl={rawUrl} file={file} mime="application/pdf" />
      ) : isDocx ? (
        // docx：前端按文档自身样式渲染（字体/表格/图片/分页），接近 Word 观感
        <DocxPreview rawUrl={rawUrl} file={file} />
      ) : isPptx && file.content_text ? (
        // pptx：后端抽取的逐页文本（# 第 N 页 分页标记）→ 讲义式分页卡片，不再是源码观感
        <PptxTextPreview text={file.content_text} />
      ) : (
        <ScrollArea className="min-h-0 flex-1">
          <pre className="whitespace-pre-wrap break-all p-4 text-xs leading-relaxed text-muted-foreground">
            {file.content_text
              || (isLegacyDoc
                // .doc 是 OLE 二进制老格式：前端无可用渲染器、后端也不抽文本，只能引导下载/转 docx
                ? '（老格式 .doc 暂不支持在线排版预览，请下载后用 Word 打开，或另存为 .docx 后重新上传）'
                : '（该文件类型暂不支持预览）')}
          </pre>
        </ScrollArea>
      )}
    </div>
  )
}

export default PreviewPanel

// M25：docx 预览——fetchRaw 拿字节（Bearer 鉴权），docx-preview 前端高保真渲染
// （页面/字体/表格/图片按文档自身样式还原；内嵌图片由库转 data URI，无需二次鉴权请求）。
// 动态 import 代码分割，不进首屏 bundle（同 ExcelPreview 范式）。
function DocxPreview({ rawUrl, file }) {
  const containerRef = useRef(null)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(false)

  useEffect(() => {
    let cancelled = false
    setError(null)
    setDone(false)
    ;(async () => {
      try {
        const buf = await fetchRaw(rawUrl)
        const { renderAsync } = await import('docx-preview')
        const el = containerRef.current
        if (cancelled || !el) return
        el.innerHTML = ''
        // inWrapper：每页套灰底卡片，接近 Word 阅读模式观感
        await renderAsync(buf, el, null, { className: 'docx-page', inWrapper: true, breakPages: true })
        if (!cancelled) setDone(true)
      } catch (e) {
        if (!cancelled) setError(e.message || String(e))
      }
    })()
    return () => { cancelled = true }
  }, [rawUrl])

  return (
    <div className="min-h-0 flex-1 overflow-auto bg-muted/30">
      {!done && !error && (
        <div className="flex items-center justify-center gap-2 p-6 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> 正在渲染文档…
        </div>
      )}
      {error && (
        <div className="p-6 text-center text-sm text-muted-foreground">
          Word 文档渲染失败（{error}），请下载后用 Word/WPS 打开
        </div>
      )}
      <div ref={containerRef} title={file.filename} />
    </div>
  )
}

// pptx 文本速览：按「# 第 N 页」标记切成一页一张卡片（讲义视图）。
// 每页首行当标题，其余逐行列出；解析不到分页标记时退化为整块文本。
function PptxTextPreview({ text }) {
  const pages = []
  const re = /^# 第 (\d+) 页\s*$/gm
  const marks = [...text.matchAll(re)]
  for (let i = 0; i < marks.length; i++) {
    const start = marks[i].index + marks[i][0].length
    const end = i + 1 < marks.length ? marks[i + 1].index : text.length
    const lines = text.slice(start, end).split('\n').map((l) => l.trim()).filter(Boolean)
    pages.push({ n: marks[i][1], lines })
  }
  if (!pages.length) {
    return <pre className="whitespace-pre-wrap break-all p-4 text-xs leading-relaxed text-muted-foreground">{text}</pre>
  }
  return (
    <div className="min-h-0 flex-1 overflow-auto bg-muted/30 p-4">
      <div className="mx-auto max-w-2xl space-y-3">
        {pages.map((p) => (
          <div key={p.n} className="rounded-lg border bg-background p-4 shadow-sm">
            <div className="mb-2 flex items-center gap-2">
              <span className="rounded bg-muted px-1.5 py-0.5 text-xs text-muted-foreground">第 {p.n} 页</span>
            </div>
            {p.lines.length > 0 && <div className="text-sm font-medium leading-snug">{p.lines[0]}</div>}
            {p.lines.length > 1 && (
              <ul className="mt-1.5 space-y-1">
                {p.lines.slice(1).map((l, i) => (
                  <li key={i} className="flex gap-1.5 text-xs leading-relaxed text-muted-foreground">
                    <span className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-muted-foreground/50" />
                    <span className="min-w-0 break-all">{l}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
        <p className="pb-2 text-center text-xs text-muted-foreground">内容速览（逐页文字提取）；排版效果请下载后用 WPS/Office 打开</p>
      </div>
    </div>
  )
}

// HTML 预览：缩放控件（整页比例缩放，横向超宽的网页版 PPT 能看全）+ 新窗口打开
const HTML_ZOOMS = [1, 0.75, 0.5, 0.33]

function HtmlPreview({ rawUrl, file }) {
  const [url, setUrl] = useState(null)
  const [zoom, setZoom] = useState(1)
  useEffect(() => {
    let objUrl = null
    let cancelled = false
    fetchRaw(rawUrl)
      .then((buf) => {
        if (cancelled) return
        objUrl = URL.createObjectURL(new Blob([buf], { type: 'text/html' }))
        setUrl(objUrl)
      })
      .catch(() => {})
    return () => {
      cancelled = true
      if (objUrl) URL.revokeObjectURL(objUrl)
    }
  }, [rawUrl])
  if (!url) return <div className="flex flex-1 items-center justify-center"><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-1 border-b px-2 py-1">
        <ZoomOut className="h-3.5 w-3.5 text-muted-foreground" />
        {HTML_ZOOMS.map((z) => (
          <button
            key={z}
            onClick={() => setZoom(z)}
            className={cn(
              'rounded px-1.5 py-0.5 text-xs',
              zoom === z ? 'bg-foreground font-medium text-background' : 'text-muted-foreground hover:bg-accent',
            )}
          >
            {Math.round(z * 100)}%
          </button>
        ))}
        <span className="ml-1 hidden text-xs text-muted-foreground sm:inline">内容过宽时调小缩放可看全</span>
        <Button variant="ghost" size="sm" className="ml-auto h-6 px-2 text-xs" onClick={() => window.open(url, '_blank')}>
          <ExternalLink className="mr-1 h-3 w-3" />
          新窗口打开
        </Button>
      </div>
      <iframe
        src={url}
        title={file.filename}
        sandbox="allow-scripts"
        className="min-h-0 flex-1 border-0 bg-white"
        style={{ zoom }}
      />
    </div>
  )
}

// 拉原始字节 → blob URL → iframe 渲染（PDF 原生查看器）
function RawFrame({ rawUrl, file, mime, sandbox }) {
  const [url, setUrl] = useState(null)
  useEffect(() => {
    let objUrl = null
    let cancelled = false
    fetchRaw(rawUrl)
      .then((buf) => {
        if (cancelled) return
        objUrl = URL.createObjectURL(new Blob([buf], { type: mime }))
        setUrl(objUrl)
      })
      .catch(() => {})
    return () => {
      cancelled = true
      if (objUrl) URL.revokeObjectURL(objUrl)
    }
  }, [rawUrl, mime])
  if (!url) return <div className="flex flex-1 items-center justify-center"><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>
  return <iframe src={url} title={file.filename} sandbox={sandbox} className="min-h-0 flex-1 border-0 bg-white" />
}

const colLetter = (i) => {
  let s = ''
  for (i += 1; i > 0; i = Math.floor((i - 1) / 26)) s = String.fromCharCode(65 + ((i - 1) % 26)) + s
  return s
}

const MAX_PREVIEW_ROWS = 1000

function ExcelPreview({ rawUrl, file }) {
  const [parsed, setParsed] = useState(null) // {XLSX, wb}
  const [sheet, setSheet] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    let cancelled = false
    setParsed(null)
    setError(null)
    ;(async () => {
      try {
        const buf = await fetchRaw(rawUrl)
        const XLSX = await import('xlsx')
        const wb = XLSX.read(buf, { type: 'array' })
        if (cancelled) return
        setParsed({ XLSX, wb })
        setSheet(wb.SheetNames[0])
      } catch (e) {
        if (!cancelled) setError(e.message)
      }
    })()
    return () => { cancelled = true }
  }, [rawUrl])

  if (error) {
    return <div className="flex flex-1 items-center justify-center p-6 text-sm text-muted-foreground">Excel 解析失败：{error}</div>
  }
  if (!parsed || !sheet) {
    return <div className="flex flex-1 items-center justify-center gap-2 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" /> 正在解析表格…</div>
  }

  const { XLSX, wb } = parsed
  const rows = XLSX.utils.sheet_to_json(wb.Sheets[sheet], { header: 1, defval: '', raw: false })
  const shown = rows.slice(0, MAX_PREVIEW_ROWS)
  const cols = Math.min(Math.max(0, ...shown.map((r) => r.length)), 100)

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* sheet 标签页 */}
      <div className="flex shrink-0 gap-1 overflow-x-auto border-b bg-muted/30 px-2 py-1.5">
        {wb.SheetNames.map((n) => (
          <button
            key={n}
            onClick={() => setSheet(n)}
            className={cn(
              'shrink-0 rounded-t border-x border-t px-2.5 py-1 text-xs',
              n === sheet ? 'border-border bg-background font-medium' : 'border-transparent text-muted-foreground hover:text-foreground',
            )}
          >
            {n}
          </button>
        ))}
      </div>
      {/* 原生 overflow-auto：Radix ScrollArea 不渲染横向 ScrollBar 时会内联 overflow-x:hidden 裁掉宽表 */}
      <div className="min-h-0 flex-1 overflow-auto">
        <table className="border-collapse text-xs">
          <thead className="sticky top-0 z-10">
            <tr>
              <th className="border bg-muted px-2 py-1 font-normal text-muted-foreground" />
              {Array.from({ length: cols }).map((_, i) => (
                <th key={i} className="min-w-16 border bg-muted px-2 py-1 font-medium text-muted-foreground">{colLetter(i)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {shown.map((r, ri) => (
              <tr key={ri}>
                <td className="border bg-muted px-2 py-1 text-right font-normal text-muted-foreground">{ri + 1}</td>
                {Array.from({ length: cols }).map((_, ci) => (
                  <td key={ci} className="whitespace-pre border px-2 py-1">{r[ci] === undefined || r[ci] === null ? '' : String(r[ci])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
        {rows.length > MAX_PREVIEW_ROWS && (
          <div className="p-3 text-center text-xs text-muted-foreground">仅预览前 {MAX_PREVIEW_ROWS} 行（共 {rows.length} 行）</div>
        )}
        {rows.length === 0 && <div className="p-6 text-center text-xs text-muted-foreground">（空表）</div>}
      </div>
    </div>
  )
}

function RawImage({ rawUrl, file }) {
  const [url, setUrl] = useState(null)
  useEffect(() => {
    let objUrl = null
    let cancelled = false
    fetchRaw(rawUrl)
      .then((buf) => {
        if (cancelled) return
        objUrl = URL.createObjectURL(new Blob([buf], { type: file.mime || 'image/png' }))
        setUrl(objUrl)
      })
      .catch(() => {})
    return () => {
      cancelled = true
      if (objUrl) URL.revokeObjectURL(objUrl)
    }
  }, [rawUrl, file.mime])
  if (!url) return <div className="flex flex-1 items-center justify-center"><Loader2 className="h-4 w-4 animate-spin text-muted-foreground" /></div>
  return (
    <div className="min-h-0 flex-1 overflow-auto">
      <div className="flex justify-center p-4"><img src={url} alt={file.filename} className="max-w-full rounded-md border" /></div>
    </div>
  )
}
