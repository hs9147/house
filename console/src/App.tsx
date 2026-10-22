import type { ReactNode } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import { isAdmin, isLoggedIn } from './lib/auth';
import Accounts from './pages/Accounts';
import AgentPlanning from './pages/AgentPlanning';
import Audit from './pages/Audit';
import AuditAssessmentTab from './pages/audit/AssessmentTab';
import AuditDashboardTab from './pages/audit/DashboardTab';
import AuditLogTab from './pages/audit/LogTab';
import Dashboard from './pages/Dashboard';
import Git from './pages/Git';
import Login from './pages/Login';
import AgentReceive from './pages/AgentReceive';
import Modules from './pages/Modules';
import Organizations from './pages/Organizations';
import ProjectDetail from './pages/ProjectDetail';
import Projects from './pages/Projects';
import PowerShellConsole from './pages/PowerShellConsole';
import Providers from './pages/Providers';
import ServerConfig from './pages/ServerConfig';
import Ontology from './pages/Ontology';
import OntologyConversionTab from './pages/ontology/ConversionTab';
import WorkflowDetail from './pages/WorkflowDetail';
import Smartwork from './pages/Smartwork';
import Workflows from './pages/Workflows';
import OntologyExploreTab from './pages/ontology/ExploreTab';
import OntologyWorkflowTab from './pages/ontology/WorkflowTab';
import Storage from './pages/Storage';
import SourceDetail from './pages/SourceDetail';
import Sources from './pages/Sources';
import DeploymentsTab from './pages/project/DeploymentsTab';
import EnvTab from './pages/project/EnvTab';
import LogsTab from './pages/project/LogsTab';
import ModulesTab from './pages/project/ModulesTab';
import ModelTab from './pages/project/ModelTab';
import ModuleReportTab from './pages/project/ModuleReportTab';
import OverviewTab from './pages/project/OverviewTab';

function RequireAuth({ children }: { children: ReactNode }) {
  if (!isLoggedIn()) return <Navigate to="/login" replace />;
  return <>{children}</>;
}

function AdminOnly({ children }: { children: ReactNode }) {
  if (!isAdmin()) return <Navigate to="/projects" replace />;
  return <>{children}</>;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      {/* 통합 북마크 「GPAX」가 여는 받는 창 — 다른 사이트가 연 창이라 로그인이 따라오지 않는다(pages/AgentReceive).
          /mail-receive는 예전 「GPAX 메일」 북마크가 여는 주소다. */}
      <Route path="/agent" element={<AgentReceive />} />
      <Route path="/mail-receive" element={<AgentReceive />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route
          path="/"
          element={
            <AdminOnly>
              <Dashboard />
            </AdminOnly>
          }
        />
        <Route path="/smartwork" element={<Smartwork />} />
        <Route path="/projects" element={<Projects />} />
        <Route path="/git" element={<Git />} />
        <Route path="/projects/:id" element={<ProjectDetail />}>
          <Route index element={<Navigate to="overview" replace />} />
          <Route path="overview" element={<OverviewTab />} />
          <Route path="model" element={<ModelTab />} />
          <Route path="deployments" element={<DeploymentsTab />} />
          <Route path="logs" element={<LogsTab />} />
          <Route path="env" element={<EnvTab />} />
          <Route path="modules" element={<ModulesTab />} />
          <Route path="module-report" element={<ModuleReportTab />} />
        </Route>
        <Route
          path="/modules"
          element={
            <AdminOnly>
              <Modules />
            </AdminOnly>
          }
        />
        <Route
          path="/accounts"
          element={
            <AdminOnly>
              <Accounts />
            </AdminOnly>
          }
        />
        <Route
          path="/storage"
          element={
            <AdminOnly>
              <Storage />
            </AdminOnly>
          }
        />
        {/* 저장소 관리 다음 자리 — 웹사이트·API·MCP를 스캔해 저장소로 옮긴다. */}
        <Route
          path="/sources"
          element={
            <AdminOnly>
              <Sources />
            </AdminOnly>
          }
        />
        <Route
          path="/sources/:id"
          element={
            <AdminOnly>
              <SourceDetail />
            </AdminOnly>
          }
        />
        {/* 문서가 그래프로 얼마나 옮겨졌는지 보고(전환 현황),
            옮겨진 것을 들여다본다(정보 조회). */}
        <Route
          path="/ontology"
          element={
            <AdminOnly>
              <Ontology />
            </AdminOnly>
          }
        >
          {/* 기본은 정보 조회 — 평소에 쓰는 일이 그것이다. 전환 현황은 고칠 때 본다. */}
          <Route index element={<OntologyExploreTab />} />
          <Route path="workflows" element={<OntologyWorkflowTab />} />
          <Route path="conversion" element={<OntologyConversionTab />} />
          {/* 옛 주소는 살려 둔다 — 북마크·공유 링크가 깨지면 "없어졌다"로 읽힌다. */}
          <Route path="explore" element={<Navigate to="/ontology" replace />} />
        </Route>
        {/* 온톨로지 다음 자리 — 조직이 플랫폼 자원과 사람 작업을 엮어 돌리는 흐름.
            소속 조직의 사용자가 직접 다룬다(다른 조직 것은 서버가 404로 숨긴다). */}
        <Route path="/workflows" element={<Workflows />} />
        {/* 스마트워크가 잠시 워크플로 관리의 탭이었을 때의 주소 — 북마크를 깨지 않고 돌려보낸다. */}
        <Route path="/workflows/smartwork" element={<Navigate to="/smartwork" replace />} />
        <Route path="/workflows/:id" element={<WorkflowDetail />} />
        <Route
          path="/server-config"
          element={
            <AdminOnly>
              <ServerConfig />
            </AdminOnly>
          }
        />
        <Route
          path="/orgs"
          element={
            <AdminOnly>
              <Organizations />
            </AdminOnly>
          }
        />
        <Route
          path="/providers"
          element={
            <AdminOnly>
              <Providers />
            </AdminOnly>
          }
        />
        <Route path="/planning" element={<AgentPlanning />} />
        {/* 사람이 지시한 일(기록), 모델 호출의 숫자(대시보드), 사람이 매긴 점수(평가). */}
        <Route
          path="/audit"
          element={
            <AdminOnly>
              <Audit />
            </AdminOnly>
          }
        >
          {/* 기본은 기록 — 여태 이 주소가 보여 준 화면이다(북마크가 깨지지 않는다). */}
          <Route index element={<AuditLogTab />} />
          <Route path="dashboard" element={<AuditDashboardTab />} />
          <Route path="assessment" element={<AuditAssessmentTab />} />
        </Route>
        <Route
          path="/powershell"
          element={
            <AdminOnly>
              <PowerShellConsole />
            </AdminOnly>
          }
        />
      </Route>
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}
