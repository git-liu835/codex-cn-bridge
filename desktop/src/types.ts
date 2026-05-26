export interface ProxyStatus {
  running: boolean;
  host: string;
  port: number;
  version: string;
  stats: {
    uptime_seconds: number;
    request_count: number;
    success_count: number;
    error_count: number;
    avg_latency_ms: number;
  };
}

export interface ModelConfig {
  alias: string;
  target_model: string;
  provider: string;
  adapter: string;
  base_url: string;
  api_key_env: string;
  api_key_set: boolean;
  enabled: boolean;
  is_multimodal: boolean;
  vision_alias: string;
  is_image_gen: boolean;
  image_gen_alias: string;
  is_video_gen: boolean;
  video_gen_alias: string;
  available_adapters: string[];
  _index?: number;
  _active?: boolean;
}

export interface ServerSettings {
  server: {
    host: string;
    port: number;
    log_level: string;
    auto_start: boolean;
    close_to_tray: boolean;
    audit_log_path: string;
    update_mirror: string;
  };
  config_path: string;
  update_mirror?: string;
}

export interface RequestLogEntry {
  timestamp: number;
  time: string;
  model: string;
  endpoint: string;
  status_code: number;
  elapsed_ms: number;
  tokens: number;
  error: string;
  stream: boolean;
  provider: string;
  target_model: string;
}

export interface TestResult {
  status: 'ok' | 'error';
  elapsed_ms?: number;
  message: string;
}

export interface DetailLogEntry {
  timestamp: number;
  time: string;
  method: string;
  path: string;
  query: string;
  request_body: string;
  response_status: number;
  response_body: string;
  elapsed_ms: number;
}
