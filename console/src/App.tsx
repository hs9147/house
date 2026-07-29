import type { ReactNode } from 'react';
import { Navigate, Route, Routes } from 'react-router-dom';
import Layout from './components/Layout';
import { isAdmin, isLoggedIn } from './lib/auth';
import Accounts from './pages/Accounts';
import Audit from './pages/Audit';
import Chat from './pages/Chat';
import Dashboard from './pages/Dashboard';
import Git from './pages/Git';
import Login from './pages/Login';
import Modules from './pages/Modules';
import Organizations from './pages/Organizations';
import ProjectDetail from './pages/ProjectDetail';
import Projects from './pages/Projects';
import PowerShellConsole from './pages/PowerShellConsole';
import Providers from './pages/Providers';
import ServerConfig from './pages/ServerConfig';
import Storage from './pages/Storage';
import CodeTab from './pages/project/CodeTab';
import DeploymentsTab from './pages/project/DeploymentsTab';
import EnvTab from './pages/project/EnvTab';
import LogsTab from './pages/project/LogsTab';
import ModulesTab from './pages/project/ModulesTab';
import ModuleReportTab from './pages/project/ModuleReportTab';
import OverviewTab from './pages/project/OverviewTab';
import PreviewsTab from './pages/project/PreviewsTab';

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
        <Route path="/projects" element={<Projects />} />
        <Route path="/git" element={<Git />} />
        <Route path="/projects/:id" element={<ProjectDetail />}>
          <Route index element={<Navigate to="overview" replace />} />
          <Route path="overview" element={<OverviewTab />} />
          <Route path="code" element={<CodeTab />} />
          <Route path="deployments" element={<DeploymentsTab />} />
          <Route path="logs" element={<LogsTab />} />
          <Route path="env" element={<EnvTab />} />
          <Route path="modules" element={<ModulesTab />} />
          <Route path="module-report" element={<ModuleReportTab />} />
          <Route path="previews" element={<PreviewsTab />} />
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
        <Route path="/chat" element={<Chat />} />
        <Route
          path="/audit"
          element={
            <AdminOnly>
              <Audit />
            </AdminOnly>
          }
        />
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
