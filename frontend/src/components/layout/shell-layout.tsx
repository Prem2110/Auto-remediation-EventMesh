import { useEffect, useState } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { useAppStore } from "../../store/app-store.ts";
import { fetchCurrentUser, fetchAllHistory } from "../../services/api.ts";
import type { IUser, IHistoryEntry } from "../../types/index.ts";
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
  // { key: "/agents",        label: "Agent Cards",   icon: "agents"        },  // hidden
  { key: "/orchestrator",  label: "Orchestrator",  icon: "orchestrator"  },
  // { key: "/test-suite",    label: "Test Suite",    icon: "test-suite"    },  // hidden
  { key: "/observability", label: "Observability", icon: "observability" },
  { key: "/pipeline",      label: "Pipeline",      icon: "pipeline"      },
];

interface ShellLayoutProps {
  children: React.ReactNode;
}

export default function ShellLayout({ children }: ShellLayoutProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const { user, setUser, history, setHistory } = useAppStore();
  const [collapsed, setCollapsed] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(true);

  useEffect(() => {
    fetchCurrentUser()
      .then((data) => {
        const u = data as IUser;
        setUser(u.email ? u : MOCK_USER);
      })
      .catch(() => setUser(MOCK_USER));
  }, [setUser]);

  useEffect(() => {
    if (!user.email || user.email === MOCK_USER.email) return;
    fetchAllHistory(user.email)
      .then((data) => setHistory((data.history || []) as IHistoryEntry[]))
      .catch(() => setHistory([]));
  }, [user.email, setHistory]);

  function isActive(key: string) {
    return location.pathname.startsWith(key);
  }

  const initials =
    (user.firstname?.[0] ?? "A") + (user.lastname?.[0] ?? "U");

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
              <span className={styles.logoSub}>Integration Suite</span>
            </span>
          )}
        </span>

        <span className={styles.topBarSpacer} />

        <div className={styles.userPill}>
          <div className={styles.userAvatar}>{initials}</div>
          {!collapsed && (
            <span className={styles.userName}>
              {user.firstname} {user.lastname}
            </span>
          )}
        </div>
      </header>

      <div className={styles.body}>
        {/* ── Sidebar ── */}
        <aside className={`${styles.sidebar} ${collapsed ? styles.sidebarCollapsed : ""}`}>
          {!collapsed && <div className={styles.navGroupLabel}>Navigation</div>}

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

          {/* <hr className={styles.navDivider} /> */}{/* hidden — History section */}

          {/* History — hidden */}
          {false && (
            <>
              <hr className={styles.navDivider} />
              <div
                className={styles.navItem}
                onClick={() => !collapsed && setHistoryOpen((o) => !o)}
                title={collapsed ? "History" : undefined}
              >
                <span className={styles.navIcon}><SvgIcon name="history" size={17} /></span>
                <span className={styles.navText}>
                  History {!collapsed && <SvgIcon name={historyOpen ? "chevron-down" : "chevron-right"} size={13} style={{ marginLeft: 2 }} />}
                </span>
              </div>

              {historyOpen && !collapsed &&
                history.map((entry) => (
                  <div
                    key={entry.id}
                    className={`${styles.navSubItem} ${
                      location.pathname === `/orchestrator/${entry.id}` ? styles.navSubItemActive : ""
                    }`}
                    onClick={() => navigate(`/orchestrator/${entry.id}`)}
                    title={entry.header}
                  >
                    <span className={styles.historyDot} />
                    <span style={{ overflow: "hidden", textOverflow: "ellipsis" }}>
                      {entry.header || entry.id}
                    </span>
                  </div>
                ))}
            </>
          )}
        </aside>

        {/* ── Main content ── */}
        <main className={styles.main}>{children}</main>
      </div>
    </div>
  );
}
