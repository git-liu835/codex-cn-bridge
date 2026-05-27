import React, { useState, useEffect, useCallback } from 'react';
import { api } from '../services/api';
import { useApp, ThemeName } from '../App';
import { Lang } from '../i18n';
import type { ServerSettings } from '../types';

const THEMES: { id: ThemeName; label: [string, string] }[] = [
  { id: 'dark',   label: ['深色', 'Dark'] },
  { id: 'light',  label: ['浅色', 'Light'] },
  { id: 'blue',   label: ['海蓝', 'Ocean'] },
  { id: 'green',  label: ['森绿', 'Forest'] },
  { id: 'purple', label: ['紫韵', 'Purple'] },
  { id: 'warm',   label: ['暖橙', 'Warm'] },
];

const Settings: React.FC = () => {
  const { theme, setTheme, lang, setLang, tl } = useApp();
  const [settings, setSettings] = useState<ServerSettings | null>(null);
  const [form, setForm] = useState({ host: '127.0.0.1', port: 8765, log_level: 'info', auto_start: false, close_to_tray: true, audit_log_path: '' });
  const [saved, setSaved] = useState(false);
  const [importYaml, setImportYaml] = useState('');

  const load = useCallback(async () => {
    try {
      const s = await api.getSettings();
      setSettings(s);
      setForm({ ...s.server });
    } catch { /* backend may not be running */ }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSave = async () => {
    try {
      await api.updateSettings(form);
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
    } catch (err: any) {
      alert(tl('common.error') + ': ' + (err.message || err));
    }
  };

  const handleExport = async () => {
    try {
      const data = await api.exportConfig();
      if (window.electronAPI) {
        await window.electronAPI.saveFile({ defaultPath: 'code-cn-bridge-config.yaml', content: data.yaml });
      } else {
        const blob = new Blob([data.yaml], { type: 'text/yaml' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'code-cn-bridge-config.yaml';
        a.click(); URL.revokeObjectURL(url);
      }
    } catch (err: any) {
      alert(tl('common.error') + ': ' + (err.message || err));
    }
  };

  const fileInputRef = React.useRef<HTMLInputElement>(null);

  const handleImport = async () => {
    if (!importYaml.trim()) {
      alert(tl('settings.emptyYaml'));
      return;
    }
    try {
      const result = await api.importConfig(importYaml);
      if (result.error) {
        alert(tl('common.error') + ': ' + result.error);
      } else {
        alert(tl('common.ok'));
        setImportYaml('');
        load();
      }
    } catch (err: any) {
      alert(tl('common.error') + ': ' + (err.message || err));
    }
  };

  const handleSelectFile = async () => {
    if (window.electronAPI) {
      try {
        const filePath = await window.electronAPI.selectFile({ filters: [{ name: 'YAML', extensions: ['yaml', 'yml'] }] });
        if (filePath) {
          const res = await fetch(`file://${filePath}`);
          const text = await res.text();
          setImportYaml(text);
        }
      } catch (err: any) {
        alert(tl('common.error') + ': ' + (err.message || err));
      }
    } else {
      fileInputRef.current?.click();
    }
  };

  const handleFileChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => setImportYaml(reader.result as string);
    reader.onerror = () => alert(tl('common.error') + ': ' + (reader.error?.message || ''));
    reader.readAsText(file);
  };

  return (
    <div className="page">
      <h2>{tl('settings.title')}</h2>

      {/* ═══ Appearance ════════════════════════════════════════ */}
      <section className="settings-section">
        <h3>{tl('settings.appearance')}</h3>
        <div className="form-grid">
          <div className="form-group">
            <label>{tl('settings.theme')}</label>
            <div className="theme-swatches">
              {THEMES.map(t => (
                <div
                  key={t.id}
                  className={`theme-swatch ${t.id} ${theme === t.id ? 'active' : ''}`}
                  title={lang === 'zh' ? t.label[0] : t.label[1]}
                  onClick={() => setTheme(t.id)}
                />
              ))}
            </div>
          </div>
          <div className="form-group">
            <label>{tl('settings.language')}</label>
            <select value={lang} onChange={e => setLang(e.target.value as Lang)}>
              <option value="zh">{tl('settings.langZh')}</option>
              <option value="en">{tl('settings.langEn')}</option>
            </select>
          </div>
        </div>
      </section>

      {/* ═══ Proxy Server ══════════════════════════════════════ */}
      <section className="settings-section">
        <h3>{tl('settings.server')}</h3>
        <div className="form-grid">
          <div className="form-group">
            <label>{tl('settings.host')}</label>
            <input value={form.host}
              onChange={e => setForm({ ...form, host: e.target.value })} />
          </div>
          <div className="form-group">
            <label>{tl('settings.port')}</label>
            <input type="number" value={form.port}
              onChange={e => setForm({ ...form, port: Number(e.target.value) })} />
          </div>
          <div className="form-group">
            <label>{tl('settings.logLevel')}</label>
            <select value={form.log_level}
              onChange={e => setForm({ ...form, log_level: e.target.value })}>
              <option value="debug">Debug</option>
              <option value="info">Info</option>
              <option value="warning">Warning</option>
              <option value="error">Error</option>
            </select>
          </div>
          <div className="form-group">
            <label>{tl('settings.auditLog')}</label>
            <input value={form.audit_log_path}
              onChange={e => setForm({ ...form, audit_log_path: e.target.value })}
              placeholder={lang === 'zh' ? '留空则不记录' : 'Leave empty to disable'} />
          </div>
        </div>

        <label className="checkbox-label">
          <input type="checkbox" checked={form.auto_start}
            onChange={e => setForm({ ...form, auto_start: e.target.checked })} />
          {tl('settings.autoStart')}
        </label>
        <label className="checkbox-label">
          <input type="checkbox" checked={form.close_to_tray}
            onChange={e => setForm({ ...form, close_to_tray: e.target.checked })} />
          {tl('settings.closeToTray')}
        </label>
      </section>

      {/* ═══ Config Management ══════════════════════════════════ */}
      <section className="settings-section">
        <h3>{tl('settings.config')}</h3>
        <div className="form-grid">
          <div className="form-group full-width">
            <label>{tl('settings.import')}</label>
            <textarea value={importYaml} onChange={e => setImportYaml(e.target.value)}
              rows={6} placeholder="YAML..."
              style={{ fontFamily: 'monospace', fontSize: '13px' }} />
            <input ref={fileInputRef} type="file" accept=".yaml,.yml"
              style={{ display: 'none' }} onChange={handleFileChange} />
            <div style={{ marginTop: 8, display: 'flex', gap: 8 }}>
              <button className="btn btn-sm" onClick={handleSelectFile}>{tl('settings.selectFile')}</button>
              <button className="btn btn-sm btn-primary" onClick={handleImport}>{tl('settings.importBtn')}</button>
            </div>
          </div>
        </div>
        <div className="btn-row" style={{ marginTop: 12 }}>
          <button className="btn btn-outline" onClick={handleExport}>{tl('settings.export')}</button>
        </div>
      </section>

      <div className="btn-row" style={{ marginTop: 24 }}>
        <button className="btn btn-primary" onClick={handleSave}>{tl('settings.save')}</button>
        {saved && <span className="save-confirm">✓ {tl('settings.saved')}</span>}
      </div>
    </div>
  );
};

export default Settings;
