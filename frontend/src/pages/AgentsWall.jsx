import { Bot, Settings, Sparkles } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'
import { api } from '@/api'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'

export default function AgentsWall() {
  const [instances, setInstances] = useState(null)
  const navigate = useNavigate()

  useEffect(() => {
    api('/agents').then((d) => setInstances(d.my_instances)).catch((e) => toast.error(e.message))
  }, [])

  return (
    <div className="h-full overflow-y-auto p-4 md:p-8">
      <div className="mx-auto max-w-5xl">
        <h1 className="text-2xl font-semibold tracking-tight">我的数字人</h1>
        <p className="mt-1 text-sm text-muted-foreground">点开对话；点齿轮调整它的人格文件与技能（只影响你自己）。新数字人和技能去左侧「市场」领用</p>
        {instances && (
          <div className="mt-6 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {instances.map((a) => (
              <Card key={a.id} className="group cursor-pointer transition-shadow hover:shadow-md" onClick={() => navigate(`/chat/${a.id}`)}>
                <CardHeader className="flex-row items-center gap-3 space-y-0">
                  <Avatar className="h-11 w-11">
                    {a.avatar ? <img src={a.avatar} alt="" /> : null}
                    <AvatarFallback><Bot className="h-5 w-5" /></AvatarFallback>
                  </Avatar>
                  <div className="min-w-0 flex-1">
                    <CardTitle className="flex items-center gap-1.5 truncate text-base">
                      {a.name}
                      {a.is_master && <Badge variant="outline" className="shrink-0 text-amber-600"><Sparkles className="mr-0.5 h-3 w-3" />主 agent</Badge>}
                    </CardTitle>
                    <CardDescription className="truncate">{a.description || '暂无描述'}</CardDescription>
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 shrink-0 opacity-100 md:opacity-0 md:group-hover:opacity-100"
                    title="管理这个数字人"
                    onClick={(e) => { e.stopPropagation(); navigate(`/my-agents/${a.id}`) }}
                  >
                    <Settings className="h-4 w-4" />
                  </Button>
                </CardHeader>
              </Card>
            ))}
            {instances.length === 0 && (
              <div className="col-span-full rounded-lg border border-dashed py-12 text-center text-sm text-muted-foreground">
                你还没有数字人，去左侧「市场」领一个吧
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  )
}
