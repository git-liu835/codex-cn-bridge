import type { ProxyStatus, ModelConfig, ServerSettings, RequestLogEntry, TestResult } from '../types';

const BASE = 'http://localhost:8765';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(body || `HTTP ${res.status}`);
  }
  return res.json();
}

export const api = {
  // 状态
  getStatus: () => request<ProxyStatus>('/admin/api/status'),

  // 模型 CRUD
  getModels: () => request<{ models: ModelConfig[] }>('/admin/api/models'),
  addModel: (data: Record<string, unknown>) =>
    request<{ status: string }>('/admin/api/models', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateModel: (alias: string, data: Record<string, unknown>, qs?: string) =>
    request<{ status: string }>(`/admin/api/models/${encodeURIComponent(alias)}${qs || ''}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteModel: (alias: string, qs?: string) =>
    request<{ status: string }>(`/admin/api/models/${encodeURIComponent(alias)}${qs || ''}`, {
      method: 'DELETE',
    }),
  activateModel: (alias: string, index: number) =>
    request<{ status: string; alias: string; active_index: number }>(
      `/admin/api/models/${encodeURIComponent(alias)}/activate/${index}`,
      { method: 'POST' }
    ),
  testConnection: (alias: string, data?: Record<string, unknown>) =>
    request<TestResult>(`/admin/api/models/${encodeURIComponent(alias)}/test`, {
      method: 'POST',
      body: JSON.stringify(data || {}),
    }),

  // 设置
  getSettings: () => request<ServerSettings>('/admin/api/settings'),
  updateSettings: (data: Record<string, unknown>) =>
    request<{ status: string; message: string }>('/admin/api/settings', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  // 日志
  getLogs: (limit = 100) => request<{ logs: RequestLogEntry[] }>(`/admin/api/logs?limit=${limit}`),
  clearLogs: () => request<{ status: string }>('/admin/api/logs/clear', { method: 'POST' }),

  // 详细日志
  getDetailedLogsStatus: () => request<{ enabled: boolean; count: number }>('/admin/api/detailed-logs/status'),
  toggleDetailedLogs: (enabled: boolean) =>
    request<{ enabled: boolean; message: string }>('/admin/api/detailed-logs/toggle', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),
  getDetailedLogs: () => request<{ logs: import('../types').DetailLogEntry[] }>('/admin/api/detailed-logs'),
  clearDetailedLogs: () => request<{ status: string }>('/admin/api/detailed-logs/clear', { method: 'POST' }),

  // 配置导入导出
  exportConfig: () => request<{ yaml: string; config_path: string }>('/admin/api/config/export'),
  importConfig: (yaml: string) =>
    request<{ status: string; error?: string }>('/admin/api/config/import', {
      method: 'POST',
      body: JSON.stringify({ yaml }),
    }),

  // Codex 配置助手（解决登录白屏/Reconnecting）
  getCodexConfig: () =>
    request<{
      config: string;
      endpoint: string;
      default_model: string;
      enabled_count: number;
      enabled_models: { alias: string; target: string; provider: string }[];
      error?: string;
    }>('/admin/api/codex-config'),
  getCodexAuth: () =>
    request<{
      auth_path: string;
      config_path: string;
      endpoint: string;
      auth_status: string;
      auth_exists: boolean;
      has_official_token: boolean;
      auth_keys: string[];
      preserve_official_auth: boolean;
      instructions: string[];
      error?: string;
    }>('/admin/api/codex-auth'),

  // Codex 配置模式切换（官方 / 桥接器）
  getCodexMode: () =>
    request<{
      mode: 'official' | 'bridge' | 'unknown';
      auth_status: string;
      mode_description: string;
      error?: string;
    }>('/admin/api/codex-mode'),
  switchCodexMode: (mode: 'official' | 'bridge') =>
    request<{
      success: boolean;
      mode: string;
      message: string;
      endpoint?: string | null;
      default_model?: string | null;
    }>('/admin/api/codex-mode', {
      method: 'POST',
      body: JSON.stringify({ mode }),
    }),

  // Codex 安装与登录状态（Models 页面顶部"官方 Codex 状态卡"使用）
  getCodexStatus: () =>
    request<{
      codex_installed: boolean;
      config_exists: boolean;
      auth_status: 'valid' | 'missing' | 'corrupted' | 'incomplete' | 'parse_error' | 'unknown';
      mode: 'official' | 'bridge' | 'unknown';
      download_url: string;
      auth_guide: string;
      error?: string;
    }>('/admin/api/codex-status'),

  // 内置 provider 预设模板（添加卡片时选预设自动填充配置）
  getProviderPresets: () =>
    request<{
      presets: Array<{
        name: string;
        label: string;
        adapter: string;
        base_url: string;
        api_key_env: string;
        docs_url: string;
        models: string[];
      }>;
    }>('/admin/api/provider-presets'),

  // 获取 /v1/models 端点返回的模型列表（Codex 桌面版会调用此端点显示模型）
  getV1Models: () =>
    request<{
      object: string;
      data: {
        id: string;
        object: string;
        owned_by: string;
        target?: string;
        context_window?: number;
        capabilities?: {
          supports_tool_calls: boolean;
          supports_streaming: boolean;
          supports_vision: boolean;
          supports_image_gen: boolean;
          supports_video_gen: boolean;
          supports_reasoning: boolean;
        };
      }[];
    }>('/v1/models'),

  // 关闭
  shutdown: () => request<{ status: string }>('/admin/api/shutdown', { method: 'POST' }),
};
