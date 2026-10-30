import { Navigate, Route, Routes } from 'react-router-dom'
import { getToken } from '@/api'
import Layout from '@/components/Layout'
import AgentsWall from '@/pages/AgentsWall'
import ChangelogPage from '@/pages/ChangelogPage'
import ChatPage from '@/pages/ChatPage'
import Login from '@/pages/Login'
import MarketPage from '@/pages/MarketPage'
import AgentsAdmin from '@/pages/admin/AgentsAdmin'
import AgentDetail from '@/pages/admin/AgentDetail'
import GatewayAdmin from '@/pages/admin/GatewayAdmin'
import RolesAdmin from '@/pages/admin/RolesAdmin'
import SkillsAdmin from '@/pages/admin/SkillsAdmin'
import StatsAdmin from '@/pages/admin/StatsAdmin'
import WecomAdmin from '@/pages/admin/WecomAdmin'
import OpsAgentDetail from '@/pages/ops/OpsAgentDetail'
import OpsConversation from '@/pages/ops/OpsConversation'
import OpsOverview from '@/pages/ops/OpsOverview'

function RequireAuth({ children }) {
  return getToken() ? children : <Navigate to="/login" replace />
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route element={<RequireAuth><Layout /></RequireAuth>}>
        <Route path="/" element={<AgentsWall />} />
        <Route path="/market" element={<MarketPage />} />
        <Route path="/changelog" element={<ChangelogPage />} />
        <Route path="/chat/:agentId" element={<ChatPage />} />
        <Route path="/chat/:agentId/:conversationId" element={<ChatPage />} />
        <Route path="/my-agents/:id" element={<AgentDetail mode="owner" />} />
        <Route path="/admin/agents" element={<AgentsAdmin />} />
        <Route path="/admin/agents/:id" element={<AgentDetail mode="admin" />} />
        <Route path="/admin/skills" element={<SkillsAdmin />} />
        <Route path="/admin/stats" element={<StatsAdmin />} />
        <Route path="/admin/gateway" element={<GatewayAdmin />} />
        <Route path="/admin/roles" element={<RolesAdmin />} />
        <Route path="/admin/wecom" element={<WecomAdmin />} />
        <Route path="/admin/ops" element={<OpsOverview />} />
        <Route path="/admin/ops/agents/:id" element={<OpsAgentDetail />} />
        <Route path="/admin/ops/conversations/:cid" element={<OpsConversation />} />
      </Route>
    </Routes>
  )
}
