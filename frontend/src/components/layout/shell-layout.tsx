import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAppStore } from "../../store/app-store.ts";
import { fetchCurrentUser } from "../../services/api.ts";
import type { IUser } from "../../types/index.ts";
import SvgIcon, { type IconName } from "../icons/SvgIcon.tsx";
import styles from "./shell-layout.module.css";

const MOCK_USER: IUser = {
  firstname: "Anonymous",
  lastname: "User",
  email: "anonymous.user@com",
  name: "dummy.user@com",
  displayName: "Dummy User",
};

const NAV_ITEMS: { key: string; label: string; icon: IconName }[] = [
  { key: "/dashboard",     label: "Dashboard",     icon: "dashboard"     },
  { key: "/observability", label: "Observability", icon: "observability" },
  { key: "/pipeline",      label: "Pipeline",      icon: "pipeline"      },
  { key: "/settings",      label: "Settings",      icon: "settings"      },
];

interface ShellLayoutProps {
  children: React.ReactNode;
}

export default function ShellLayout({ children }: ShellLayoutProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const { setUser } = useAppStore();
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    fetchCurrentUser()
      .then((data) => {
        const u = data as IUser;
        setUser(u.email ? u : MOCK_USER);
      })
      .catch(() => setUser(MOCK_USER));
  }, [setUser]);

  function isActive(key: string) {
    return location.pathname.startsWith(key);
  }

  return (
    <div className={styles.appShell}>
      {/* ── Top bar ── */}
      <header className={styles.topBar}>
        <button
          className={styles.menuBtn}
          onClick={() => setCollapsed((c) => !c)}
          aria-label="Toggle menu"
        >
          <span className={styles.menuIcon}>
            <span />
            <span />
            <span />
          </span>
        </button>

        <span className={styles.logo} onClick={() => navigate("/dashboard")} style={{ cursor: "pointer" }}>
          <span className={styles.logoMark}>O</span>
          {!collapsed && (
            <span className={styles.logoText}>
              <span className={styles.logoTitle}>Orbit</span>
              <span className={styles.logoSub}>Autonomous Integration Monitor</span>
            </span>
          )}
        </span>

        <span className={styles.topBarSpacer} />

        <div className={styles.topBarBadge}>
          <span className={styles.topBarBadgeIcon}>S</span>
          SAP CPI
        </div>

      </header>

      <div className={styles.body}>
        {/* ── Sidebar ── */}
        <aside className={`${styles.sidebar} ${collapsed ? styles.sidebarCollapsed : ""}`}>
          {NAV_ITEMS.map((item) => (
            <div
              key={item.key}
              className={`${styles.navItem} ${isActive(item.key) ? styles.navItemActive : ""}`}
              onClick={() => navigate(item.key)}
              title={collapsed ? item.label : undefined}
            >
              <span className={styles.navIcon}><SvgIcon name={item.icon} size={17} /></span>
              <span className={styles.navText}>{item.label}</span>
            </div>
          ))}
        </aside>

        {/* ── Main content ── */}
        <main className={styles.main}>{children}</main>
      </div>
    </div>
  );
}
