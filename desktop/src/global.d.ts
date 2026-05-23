export {};

declare global {
  interface Window {
    electronAPI?: {
      getBridgeStatus: () => Promise<any>;
      exportConfig: () => Promise<{ yaml: string }>;
      importConfig: (yaml: string) => Promise<any>;
      selectFile: (options?: { filters?: Array<{ name: string; extensions: string[] }> }) => Promise<string | null>;
      saveFile: (options: { defaultPath?: string; content: string }) => Promise<string | null>;
      openExternal: (url: string) => Promise<void>;
      onBridgeStatus: (callback: (status: any) => void) => void;
      onBridgeLog: (callback: (log: any) => void) => void;
      checkForUpdates: () => Promise<{ status: string; version?: string; message?: string }>;
      getAppVersion: () => Promise<string>;
      onUpdateStatus: (callback: (status: any) => void) => void;
    };
  }
}
