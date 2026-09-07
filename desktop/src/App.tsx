import React, { useState, useEffect, createContext, useContext } from 'react';
import { HashRouter, Routes, Route, NavLink, Navigate } from 'react-router-dom';
import Dashboard from './pages/Dashboard';
import Models from './pages/Models';
import Settings from './pages/Settings';
import Logs from './pages/Logs';
import About from './pages/About';
import StatusBar from './components/StatusBar';
import { api } from './services/api';
import { t, Lang } from './i18n';
import logoUrl from './assets/logo.png';

// ── Theme & Language Context ───────────────────────────────────

export type ThemeName = 'dark' | 'light' | 'blue' | 'green' | 'purple' | 'warm';

interface AppContextType {
  theme: ThemeName;
  setTheme: (t: ThemeName) => void;
  lang: Lang;
  setLang: (l: Lang) => void;
  tl: (key: Parameters<typeof t>[0]) => string;
}
export const AppContext = createContext<AppContextType>(null!);
export const useApp = () => useContext(AppContext);

function loadSetting<T>(key: string, fallback: T): T {
  try {
    const v = localStorage.getItem(`code-bridge:${key}`);
    return v ? JSON.parse(v) : fallback;
  } catch { return fallback; }
}

function saveSetting(key: string, val: unknown) {
  localStorage.setItem(`code-bridge:${key}`, JSON.stringify(val));
}

// ── App Component ──────────────────────────────────────────────

const App: React.FC = () => {
  const [proxyRunning, setProxyRunning] = useState(false);
  const [requestCount, setRequestCount] = useState(0);
  const [theme, _setTheme] = useState<ThemeName>(() => loadSetting('theme', 'dark'));
  const [lang, _setLang] = useState<Lang>(() => loadSetting('lang', 'zh'));

  const setTheme = (t: ThemeName) => { _setTheme(t); saveSetting('theme', t); };
  const setLang = (l: Lang) => { _setLang(l); saveSetting('lang', l); };

  // 应用主题到 document
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
  }, [theme]);

  useEffect(() => {
    const checkStatus = async () => {
      try {
        const status = await api.getStatus();
        setProxyRunning(status.running);
        setRequestCount(status.stats?.request_count ?? 0);
      } catch {
        setProxyRunning(false);
      }
    };
    checkStatus();
    const interval = setInterval(checkStatus, 5000);
    return () => clearInterval(interval);
  }, []);

  const ctx: AppContextType = { theme, setTheme, lang, setLang, tl: (k) => t(k, lang) };

  return (
    <HashRouter>
      <AppContext.Provider value={ctx}>
        <AppInner proxyRunning={proxyRunning} requestCount={requestCount} />
      </AppContext.Provider>
    </HashRouter>
  );
};

const AppInner: React.FC<{ proxyRunning: boolean; requestCount: number }> = ({ proxyRunning, requestCount }) => {
  const { tl } = useApp();
  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="header-brand">
          <img src={logoUrl} alt="CNB" className="header-logo-img" />
          <span className="header-title">{tl('app.title')}</span>
        </div>
        <div className="header-status">
          <span className={`status-dot ${proxyRunning ? 'running' : 'stopped'}`} />
          <span className="status-text">{proxyRunning ? tl('app.running') : tl('app.stopped')}</span>
        </div>
      </header>

      <div className="app-body">
        <nav className="sidebar">
          <NavLink to="/dashboard" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="3" width="7" height="9" rx="1.5"/><rect x="14" y="3" width="7" height="5" rx="1.5"/><rect x="14" y="12" width="7" height="9" rx="1.5"/><rect x="3" y="16" width="7" height="5" rx="1.5"/></svg>
            {tl('nav.dashboard')}
          </NavLink>
          <NavLink to="/models" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><path d="M9 1v3M15 1v3M9 20v3M15 20v3M1 9h3M1 15h3M20 9h3M20 15h3"/></svg>
            {tl('nav.models')}
          </NavLink>
          <NavLink to="/settings" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            {tl('nav.settings')}
          </NavLink>
          <NavLink to="/logs" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8M16 17H8M10 9H8"/></svg>
            {tl('nav.logs')}
          </NavLink>
          <NavLink to="/about" className={({ isActive }) => `nav-item ${isActive ? 'active' : ''}`}>
            <svg className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/></svg>
            {tl('nav.about')}
          </NavLink>
        </nav>

        <main className="content">
          <Routes>
            <Route path="/dashboard" element={<Dashboard />} />
            <Route path="/models" element={<Models />} />
            <Route path="/settings" element={<Settings />} />
            <Route path="/logs" element={<Logs />} />
            <Route path="/about" element={<About />} />
            <Route path="*" element={<Navigate to="/dashboard" replace />} />
          </Routes>
        </main>
      </div>

      <StatusBar proxyRunning={proxyRunning} port={8765} requestCount={requestCount} />
    </div>
  );
};

export default App;
