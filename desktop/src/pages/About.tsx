import React, { useState, useEffect, useCallback } from 'react';
import { api } from '../services/api';

const About: React.FC = () => {
  const [appVersion, setAppVersion] = useState('0.3.6');
  const [updateStatus, setUpdateStatus] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [configPath, setConfigPath] = useState('');
  const [updateMirror, setUpdateMirror] = useState('');

  useEffect(() => {
    window.electronAPI?.getAppVersion().then(v => {
      if (v) setAppVersion(v);
    });
    api.getSettings().then(s => {
      if (s.config_path) setConfigPath(s.config_path);
      if (s.update_mirror) setUpdateMirror(s.update_mirror);
    }).catch(() => {});
  }, []);

  const handleSaveMirror = async (url: string) => {
    setUpdateMirror(url);
    try {
      await api.updateSettings({ update_mirror: url });
    } catch { /* ignore */ }
  };

  useEffect(() => {
    window.electronAPI?.onUpdateStatus((status: any) => {
      if (status.status === 'downloading') {
        setUpdateStatus(`正在下载... ${status.percent || 0}%`);
        setChecking(false);
      } else if (status.status === 'downloaded') {
        setUpdateStatus('已下载，等待安装');
        setChecking(false);
      } else if (status.status === 'up-to-date') {
        setUpdateStatus('已是最新版本');
        setChecking(false);
      } else if (status.status === 'error') {
        setUpdateStatus(`检查失败: ${status.message || ''}`);
        setChecking(false);
      }
    });
  }, []);

  const handleCheckUpdate = useCallback(async () => {
    setChecking(true);
    setUpdateStatus('正在检查...');
    const result = await window.electronAPI?.checkForUpdates();
    if (result?.status === 'dev-mode') {
      setUpdateStatus('开发模式，跳过更新检查');
      setChecking(false);
    } else if (result?.status === 'error') {
      setUpdateStatus(`检查失败: ${result.message || ''}`);
      setChecking(false);
    }
    // 'ok' means an update was found — handled by onUpdateStatus
  }, []);

  return (
    <div className="page">
      <h2>关于 code CN Bridge</h2>

      <div className="about-card">
        <div className="about-logo">&#9653;</div>
        <h3>code CN Bridge</h3>
        <p className="version">v{appVersion}</p>
        <p className="about-desc">
          本地代理工具，将 OpenAI Responses API 翻译为 Chat Completions API，
          使 code CLI 无缝接入通义千问、DeepSeek、Kimi 等国产大模型。
        </p>

        <div className="about-update" style={{ margin: '16px 0' }}>
          <button
            className="btn btn-outline"
            onClick={handleCheckUpdate}
            disabled={checking}
            style={{ minWidth: 120 }}
          >
            {checking ? '检查中...' : '检查更新'}
          </button>
          <div style={{ marginTop: 8, display: 'flex', alignItems: 'center', gap: 8 }}>
            <input
              value={updateMirror}
              onChange={e => handleSaveMirror(e.target.value)}
              placeholder="更新镜像 (如 https://ghproxy.com)"
              style={{
                flex: 1, padding: '6px 10px', fontSize: 12,
                background: 'var(--bg-card)', border: '1px solid var(--border)',
                borderRadius: 'var(--radius)', color: 'var(--text-primary)',
              }}
            />
          </div>
          {updateStatus && (
            <div style={{ marginTop: 8, display: 'flex', alignItems: 'flex-start', gap: 8 }}>
              <code style={{
                flex: 1, padding: '8px 12px', fontSize: 13,
                background: 'var(--bg-card)', border: '1px solid var(--border)',
                borderRadius: 'var(--radius)', whiteSpace: 'pre-wrap',
                wordBreak: 'break-all', userSelect: 'text',
                color: 'var(--text-secondary)',
              }}>
                {updateStatus}
              </code>
              <button
                className="btn btn-sm"
                onClick={() => {
                  navigator.clipboard.writeText(updateStatus).catch(() => {});
                }}
                title="复制日志"
                style={{ flexShrink: 0 }}
              >
                复制
              </button>
            </div>
          )}
        </div>

        <div className="about-features">
          <div className="feature">
            <strong>协议转换</strong>
            <p>Responses API ↔ Chat Completions API 双向转换，支持流式输出</p>
          </div>
          <div className="feature">
            <strong>多模型支持</strong>
            <p>内置通义千问、DeepSeek、Kimi 适配器，支持自定义扩展</p>
          </div>
          <div className="feature">
            <strong>桌面管理</strong>
            <p>Electron 桌面应用，系统托盘驻留，图形化管理所有配置</p>
          </div>
        </div>

        <div className="about-links">
          <a href="#" onClick={(e) => {
            e.preventDefault();
            window.electronAPI?.openExternal('https://github.com/git-liu835/code-cn-bridge');
          }}>
            项目主页
          </a>
          <span className="separator">|</span>
          <a href="#" onClick={(e) => {
            e.preventDefault();
            window.electronAPI?.openExternal('https://github.com/git-liu835/code-cn-bridge/issues');
          }}>
            问题反馈
          </a>
        </div>

        {configPath && (
          <div style={{ marginTop: 16, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}>
            <span style={{ fontSize: 12, color: 'var(--text-muted)' }}>配置路径:</span>
            <code style={{
              padding: '2px 8px', fontSize: 12, background: 'var(--bg-card)',
              border: '1px solid var(--border)', borderRadius: 'var(--radius)',
              userSelect: 'text',
            }}>
              {configPath}
            </code>
          </div>
        )}
        <p className="muted" style={{ marginTop: configPath ? 8 : 16 }}>
          License: MIT | Built with Electron + React + FastAPI
        </p>
      </div>
    </div>
  );
};

export default About;
