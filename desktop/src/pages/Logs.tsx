import React, { useState, useEffect, useRef } from 'react';
import { api } from '../services/api';
import type { RequestLogEntry, DetailLogEntry } from '../types';

const Logs: React.FC = () => {
  const [logs, setLogs] = useState<RequestLogEntry[]>([]);
  const [detailedLogs, setDetailedLogs] = useState<DetailLogEntry[]>([]);
  const [detailEnabled, setDetailEnabled] = useState(false);
  const [paused, setPaused] = useState(false);
  const [connected, setConnected] = useState(false);
  const [expandedIdx, setExpandedIdx] = useState<number | null>(null);
  const wsRef = useRef<WebSocket | null>(null);

  // 初始加载
  useEffect(() => {
    (async () => {
      try {
        const res = await api.getLogs(200);
        setLogs(res.logs || []);
        const ds = await api.getDetailedLogsStatus();
        setDetailEnabled(ds.enabled);
        if (ds.enabled) {
          const dl = await api.getDetailedLogs();
          setDetailedLogs(dl.logs || []);
        }
      } catch { /* ignore */ }
    })();
  }, []);

  // WebSocket 实时日志
  useEffect(() => {
    let ws: WebSocket;
    let reconnectTimer: ReturnType<typeof setTimeout>;

    const connect = () => {
      try {
        ws = new WebSocket('ws://localhost:8765/admin/api/logs/stream');
        wsRef.current = ws;

        ws.onopen = () => setConnected(true);
        ws.onclose = () => { setConnected(false); reconnectTimer = setTimeout(connect, 3000); };
        ws.onerror = () => ws?.close();

        ws.onmessage = (event) => {
          try {
            const data = JSON.parse(event.data);
            if (data.type === 'detailed') {
              if (!paused) {
                setDetailedLogs((prev) => [data.entry, ...prev].slice(0, 100));
              }
            } else {
              if (!paused) {
                setLogs((prev) => [data, ...prev].slice(0, 500));
              }
            }
          } catch { /* ignore */ }
        };
      } catch {
        reconnectTimer = setTimeout(connect, 3000);
      }
    };

    connect();
    return () => {
      clearTimeout(reconnectTimer);
      ws?.close();
    };
  }, [paused]);

  const handleToggleDetailed = async () => {
    const next = !detailEnabled;
    setDetailEnabled(next);
    try {
      const res = await api.toggleDetailedLogs(next);
      setDetailEnabled(res.enabled);
      if (res.enabled) {
        const dl = await api.getDetailedLogs();
        setDetailedLogs(dl.logs || []);
      }
    } catch { /* ignore */ }
  };

  const handleClear = async () => {
    try {
      await api.clearLogs();
      setLogs([]);
      await api.clearDetailedLogs();
      setDetailedLogs([]);
    } catch { /* ignore */ }
  };

  return (
    <div className="page">
      <div className="page-header">
        <h2>监控日志
          <span className={`ws-indicator ${connected ? 'running' : 'stopped'}`}>
            {connected ? '实时' : '断开'}
          </span>
        </h2>
        <div className="btn-row">
          <label className="checkbox-label" style={{ marginRight: 12 }}>
            <input type="checkbox" checked={detailEnabled} onChange={handleToggleDetailed} />
            详细日志
          </label>
          <button className={`btn btn-sm ${paused ? 'btn-primary' : ''}`}
            onClick={() => setPaused(!paused)}>
            {paused ? '继续' : '暂停'}
          </button>
          <button className="btn btn-sm btn-danger" onClick={handleClear}>清空</button>
        </div>
      </div>

      <div className="log-list">
        {/* ── 详细日志视图 ─────────────────────────────── */}
        {detailEnabled && detailedLogs.length > 0 && (
          <>
            <div className="detail-log-count" style={{ marginBottom: 8, fontSize: 13, color: 'var(--text-secondary)' }}>
              详细日志（最近 100 条，自动清理）: {detailedLogs.length} 条
            </div>
            {detailedLogs.map((dlog, i) => {
              const isExpanded = expandedIdx === i;
              const isError = dlog.response_status >= 400;
              return (
                <div key={i} className={`detail-log-entry ${isError ? 'error' : ''}`}>
                  <div
                    className="detail-log-summary"
                    onClick={() => setExpandedIdx(isExpanded ? null : i)}
                    style={{ cursor: 'pointer' }}
                  >
                    <span className="log-time">{dlog.time}</span>
                    <span className={`log-badge ${isError ? 'stopped' : 'running'}`}>
                      {dlog.response_status}
                    </span>
                    <span className="log-method" style={{ fontWeight: 600, marginRight: 6 }}>
                      {dlog.method}
                    </span>
                    <span className="log-endpoint" style={{ fontFamily: 'monospace' }}>
                      {dlog.path}{dlog.query ? `?${dlog.query}` : ''}
                    </span>
                    <span className="log-elapsed">{dlog.elapsed_ms}ms</span>
                    <span style={{ marginLeft: 'auto', color: 'var(--text-secondary)', fontSize: 12 }}>
                      {isExpanded ? '收起' : '展开'}
                    </span>
                  </div>
                  {isExpanded && (
                    <div className="detail-log-body">
                      <div className="detail-section">
                        <div className="detail-label">请求体 (Request)</div>
                        <pre className="detail-code">{dlog.request_body || '(空)'}</pre>
                      </div>
                      <div className="detail-section">
                        <div className="detail-label">响应体 (Response)</div>
                        <pre className="detail-code">{dlog.response_body || '(空)'}</pre>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </>
        )}

        {/* ── 普通日志视图 ─────────────────────────────── */}
        {!detailEnabled && (
          <>
            {logs.length === 0 && <p className="muted">暂无请求日志</p>}
            {logs.map((log, i) => (
              <div key={i} className={`log-entry ${log.status_code >= 400 ? 'error' : ''}`}>
                <span className="log-time">{log.time}</span>
                <span className={`log-badge ${log.status_code < 400 ? 'running' : 'stopped'}`}>
                  {log.status_code}
                </span>
                <span className="log-model">{log.model}</span>
                {log.provider && <span className="log-provider">{log.provider}/{log.target_model}</span>}
                <span className="log-endpoint">{log.endpoint}</span>
                <span className="log-elapsed">{log.elapsed_ms}ms</span>
                {log.tokens > 0 && <span className="log-tokens">{log.tokens} tokens</span>}
                {log.error && <span className="log-error">{log.error}</span>}
              </div>
            ))}
          </>
        )}
      </div>
    </div>
  );
};

export default Logs;
