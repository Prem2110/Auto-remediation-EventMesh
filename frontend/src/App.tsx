import { Routes, Route, Navigate } from "react-router-dom";
import ShellLayout from "./components/layout/shell-layout.tsx";
import AgentCards from "./pages/agent-cards/agent-cards.tsx";
import Orchestrator from "./pages/orchestrator/orchestrator.tsx";
import Observability from "./pages/observability/observability.tsx";
import TestSuite from "./pages/test-suite/test-suite.tsx";
import MigrationWizard from "./pages/migration-wizard/migration-wizard.tsx";
import PipoList from "./pages/pipo-list/pipo-list.tsx";
import Dashboard from "./pages/dashboard/dashboard.tsx";
import Pipeline from "./pages/pipeline/pipeline.tsx";

export default function App() {
  return (
    <ShellLayout>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/agents" element={<AgentCards />} />
        <Route path="/orchestrator" element={<Orchestrator />} />
        <Route path="/orchestrator/:id" element={<Orchestrator />} />
        <Route path="/observability" element={<Observability />} />
        <Route path="/test-suite" element={<TestSuite />} />
        <Route path="/migration" element={<MigrationWizard />} />
        <Route path="/pipo" element={<PipoList />} />
        <Route path="/pipeline" element={<Pipeline />} />
      </Routes>
    </ShellLayout>
  );
}
