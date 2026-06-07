import { useEffect } from "react";
import { Routes, Route, Navigate } from "react-router-dom";
import ShellLayout from "./components/layout/shell-layout.tsx";
import Orchestrator from "./pages/orchestrator/orchestrator.tsx";
import Observability from "./pages/observability/observability.tsx";
import MigrationWizard from "./pages/migration-wizard/migration-wizard.tsx";
import PipoList from "./pages/pipo-list/pipo-list.tsx";
import Dashboard from "./pages/dashboard/dashboard.tsx";
import Pipeline from "./pages/pipeline/pipeline.tsx";
import Settings from "./pages/settings/settings.tsx";

export default function App() {
  useEffect(() => {
    const savedTheme = localStorage.getItem("orbit-theme") || "aurora";
    if (savedTheme === "plain") {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", savedTheme);
    }

    const fontSizeMap: Record<string, string> = { sm: "0.8rem", md: "0.875rem", lg: "0.95rem" };
    const savedFontSize = localStorage.getItem("orbit-font-size");
    if (savedFontSize && fontSizeMap[savedFontSize]) {
      document.documentElement.style.setProperty("--orbit-font-size-base", fontSizeMap[savedFontSize]);
    }
  }, []);

  return (
    <ShellLayout>
      <Routes>
        <Route path="/" element={<Navigate to="/dashboard" replace />} />
        <Route path="/dashboard" element={<Dashboard />} />
        <Route path="/orchestrator" element={<Orchestrator />} />
        <Route path="/orchestrator/:id" element={<Orchestrator />} />
        <Route path="/observability" element={<Observability />} />
        <Route path="/migration" element={<MigrationWizard />} />
        <Route path="/pipo" element={<PipoList />} />
        <Route path="/pipeline" element={<Pipeline />} />
        <Route path="/settings" element={<Settings />} />
      </Routes>
    </ShellLayout>
  );
}
