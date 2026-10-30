import { cn } from '@/lib/utils'

// OREO 品牌 Logo：粗圆环（字母 O = 企业体系 / 镂空 = AI 核心）+ 右下 45° 尾巴（对话气泡入口）
// 字标用自托管 Inter（Bold），中文优先苹方 PingFang SC、Windows 回落雅黑
export default function Logo({ collapsed = false, className }) {
  const size = collapsed ? 32 : 44
  return (
    <div className={cn('flex items-center gap-3', className)}>
      <svg
        width={size}
        height={size}
        viewBox="0 0 44 44"
        fill="none"
        xmlns="http://www.w3.org/2000/svg"
        className="shrink-0"
        aria-label="OREO"
      >
        {/* 圆环：中心 (21,21)，中线 r=13.5，线宽 9 → 外径 18 / 镂空内径 9 */}
        <circle cx="21" cy="21" r="13.5" strokeWidth="9" className="stroke-black dark:stroke-white" />
        {/* 尾巴：前半段与环身同粗（等宽矩形），二分之一处开始收成尖角，尖端与圆环外缘齐平 */}
        <path d="M28.07 33.02 L32.05 37 L38.5 38.5 L37 32.05 L33.02 28.07 Z" className="fill-black dark:fill-white" />
      </svg>
      {!collapsed && (
        <div className="min-w-0">
          <div className="font-logo truncate text-xl font-bold leading-none tracking-wider">OREO 奥洛</div>
        </div>
      )}
    </div>
  )
}
