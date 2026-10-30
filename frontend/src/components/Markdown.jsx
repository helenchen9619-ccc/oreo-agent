// 助手消息 markdown 渲染（M3.11）：链接可点新窗口、GFM 表格/自动链接、代码块灰底。
// 不渲染原始 HTML（react-markdown 默认安全行为，勿开 rehype-raw）。
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

const components = {
  // 站内链接（/ 开头，如公告里的 /login?upgrade=1 授权链接）当前页跳转——授权流程依赖整页重定向飞书；
  // 外链才新窗口打开
  a: ({ href, children }) => (
    <a href={href} {...(href?.startsWith('/') ? {} : { target: '_blank', rel: 'noreferrer' })}
      className="text-blue-600 underline underline-offset-2 break-all dark:text-blue-400">
      {children}
    </a>
  ),
  p: ({ children }) => <p className="my-1.5 first:mt-0 last:mb-0">{children}</p>,
  h1: ({ children }) => <h1 className="mb-2 mt-3 text-base font-bold first:mt-0">{children}</h1>,
  h2: ({ children }) => <h2 className="mb-1.5 mt-3 text-[15px] font-bold first:mt-0">{children}</h2>,
  h3: ({ children }) => <h3 className="mb-1 mt-2.5 text-sm font-semibold first:mt-0">{children}</h3>,
  h4: ({ children }) => <h4 className="mb-1 mt-2 text-sm font-semibold first:mt-0">{children}</h4>,
  ul: ({ children }) => <ul className="my-1.5 list-disc space-y-0.5 pl-5">{children}</ul>,
  ol: ({ children }) => <ol className="my-1.5 list-decimal space-y-0.5 pl-5">{children}</ol>,
  blockquote: ({ children }) => (
    <blockquote className="my-1.5 border-l-2 border-muted-foreground/30 pl-3 text-muted-foreground">{children}</blockquote>
  ),
  hr: () => <hr className="my-3 border-border" />,
  strong: ({ children }) => <strong className="font-semibold">{children}</strong>,
  table: ({ children }) => (
    <div className="my-2 overflow-x-auto rounded-md border">
      <table className="w-full border-collapse text-[13px]">{children}</table>
    </div>
  ),
  th: ({ children }) => <th className="border-b bg-muted px-2.5 py-1.5 text-left font-medium">{children}</th>,
  td: ({ children }) => <td className="border-b px-2.5 py-1.5 align-top last:border-b-0">{children}</td>,
  // 行内 code 灰底；pre 里的 code 由 [&>code] 复位避免双重底色
  code: ({ className, children }) => (
    <code className={`rounded bg-muted px-1 py-0.5 text-[0.85em] ${className || ''}`}>{children}</code>
  ),
  pre: ({ children }) => (
    <pre className="my-2 overflow-x-auto rounded-md bg-muted p-3 text-xs leading-relaxed [&>code]:bg-transparent [&>code]:p-0">{children}</pre>
  ),
}

export default function Markdown({ children }) {
  return (
    <div className="break-words text-sm leading-relaxed">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
        {children || ''}
      </ReactMarkdown>
    </div>
  )
}
