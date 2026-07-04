import React, { useState, useEffect, useCallback } from 'react';
import { api } from '../services/api';
import type { ProxyStatus, ModelConfig } from '../types';

type CodexMode = 'official' | 'bridge' | 'unknown';

const Dashboard: React.FC = () => {
  const [status, setStatus] = useState<ProxyStatus | null>(null);
  const [models, setModels] = useState<ModelConfig[]>([]);
  const [codexMode, setCodexMode] = useState<CodexMode>('unknown');
  const [authStatus, setAuthStatus] = useState<string>('unknown');
  const [modeDescription, setModeDescription] = useState<string>('');
  const [switching, setSwitching] = useState(false);
  const [modeMessage, setModeMessage] = useState<string>('');

  const load = useCallback(async () => {
    try {
      const [s, m] = await Promise.all([api.getStatus(), api.getModels()]);
      setStatus(s);
      setModels(m.models || []);
    } catch {
      setStatus(null);
    }
    // 单独加载 Codex 模式（失败不阻塞仪表板）
    try {
      const modeData = await api.getCodexMode();
      setCodexMode(modeData.mode);
      setAuthStatus(modeData.auth_status);
      setModeDescription(modeData.mode_description || '');
    } catch {
      // bridge 未运行时静默
    }
  }, []);

  useEffect(() => { load(); }, [load]);

  const handleSwitchMode = useCallback(async (target: 'official' | 'bridge') => {
    if (target === codexMode || switching) return;
    setSwitching(true);
    setModeMessage('');
    try {
      const result = await api.switchCodexMode(target);
      if (result.success) {
        setCodexMode(target);
        setModeMessage(result.message);
        // 切换到 bridge 模式时，重新加载模式描述
        try {
          const modeData = await api.getCodexMode();
          setCodexMode(modeData.mode);
          setAuthStatus(modeData.auth_status);
          setModeDescription(modeData.mode_description || '');
        } catch {
          // ignore
        }
      } else {
        setModeMessage(result.message || '切换失败');
      }
    } catch (e) {
      setModeMessage(`切换失败: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setSwitching(false);
      // 3 秒后清空消息
      setTimeout(() => setModeMessage(''), 3000);
    }
  }, [codexMode, switching]);

  const uptime = status ? Math.floor(status.stats.uptime_seconds) : 0;
  const uptimeStr = `${Math.floor(uptime / 3600)}h ${Math.floor((uptime % 3600) / 60)}m ${uptime % 60}s`;

  // auth 状态中文映射
  const authStatusText: Record<string, string> = {
    valid: '官方登录正常',
    missing: '未登录',
    corrupted: '登录态损坏',
    incomplete: '登录态不完整',
    parse_error: '登录态解析失败',
    unknown: '未知',
  };
  const authStatusClass = authStatus === 'valid' ? 'success' : (authStatus === 'missing' || authStatus === 'corrupted' ? 'error' : 'warn');

  return (
    <div className="page">
      <h2>仪表板</h2>

      <div className="cards-row">
        <div className="card status-card">
          <h3>代理状态</h3>
          <div className={`status-badge ${status?.running ? 'running' : 'stopped'}`}>
            {status?.running ? '运行中' : '已停止'}
          </div>
          {status?.running && (
            <div className="card-detail">
              <div>运行时间: {uptimeStr}</div>
              <div>版本: {status.version}</div>
            </div>
          )}
        </div>

        <div className="card stats-card">
          <h3>请求统计</h3>
          <div className="stats-grid">
            <div className="stat">
              <span className="stat-num">{status?.stats.request_count ?? 0}</span>
              <span className="stat-label">总请求</span>
            </div>
            <div className="stat success">
              <span className="stat-num">{status?.stats.success_count ?? 0}</span>
              <span className="stat-label">成功</span>
            </div>
            <div className="stat error">
              <span className="stat-num">{status?.stats.error_count ?? 0}</span>
              <span className="stat-label">失败</span>
            </div>
            <div className="stat">
              <span className="stat-num">{status?.stats.avg_latency_ms ?? 0}ms</span>
              <span className="stat-label">平均延迟</span>
            </div>
          </div>
        </div>
      </div>

      {/* Codex 配置模式切换器 */}
      <div className="card mode-switcher-card">
        <h3>Codex 配置模式</h3>
        <div className="mode-switcher-body">
          <div className="mode-current">
            <span className="mode-label">当前模式：</span>
            <span className={`mode-badge mode-${codexMode}`}>
              {codexMode === 'official' ? '官方' : codexMode === 'bridge' ? '桥接器' : '未知'}
            </span>
            <span className={`auth-status auth-${authStatusClass}`}>
              {authStatusText[authStatus] || authStatus}
            </span>
          </div>
          {modeDescription && <p className="mode-desc muted">{modeDescription}</p>}
          <div className="mode-buttons">
            <button
              className={`btn ${codexMode === 'official' ? 'btn-active' : 'btn-secondary'}`}
              onClick={() => handleSwitchMode('official')}
              disabled={switching || codexMode === 'official'}
            >
              官方模式
            </button>
            <button
              className={`btn ${codexMode === 'bridge' ? 'btn-active' : 'btn-secondary'}`}
              onClick={() => handleSwitchMode('bridge')}
              disabled={switching || codexMode === 'bridge'}
            >
              桥接器模式
            </button>
          </div>
          {switching && <p className="mode-msg">切换中...</p>}
          {modeMessage && <p className={`mode-msg ${modeMessage.includes('失败') ? 'error' : 'success'}`}>{modeMessage}</p>}
          {codexMode === 'bridge' && authStatus !== 'valid' && (
            <p className="mode-msg warn">
              桥接器模式需要官方登录态才能使用插件功能，请先用 ChatGPT 账号登录 Codex
            </p>
          )}
        </div>
      </div>

      <h3>模型健康状态</h3>
      <div className="model-health-grid">
        {models.length === 0 && <p className="muted">尚未配置任何模型，前往"模型配置"添加。</p>}
        {models.map((m) => (
          <div key={m.alias} className="health-card">
            <div className="health-card-header">
              <span className={`dot ${m.enabled ? 'running' : 'stopped'}`} />
              <strong>{m.alias}</strong>
            </div>
            <div className="health-card-body">
              <div>{m.target_model}</div>
              <div className="muted">{m.provider} / {m.adapter}</div>
            </div>
          </div>
        ))}
      </div>

      <div className="quick-actions">
        <button className="btn btn-primary" onClick={load}>刷新状态</button>
      </div>
    </div>
  );
};

export default Dashboard;
