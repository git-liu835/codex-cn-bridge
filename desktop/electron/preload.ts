import { contextBridge, ipcRenderer } from 'electron';

contextBridge.exposeInMainWorld('electronAPI', {
  getBridgeStatus: () => ipcRenderer.invoke('get-bridge-status'),
  exportConfig: () => ipcRenderer.invoke('export-config'),
  importConfig: (yaml: string) => ipcRenderer.invoke('import-config', yaml),
  selectFile: (options?: any) => ipcRenderer.invoke('select-file', options || {}),
  saveFile: (options: { defaultPath?: string; content: string }) => ipcRenderer.invoke('save-file', options),
  openExternal: (url: string) => ipcRenderer.invoke('open-external', url),
  onBridgeStatus: (callback: (status: any) => void) => {
    ipcRenderer.on('bridge-status', (_event, status) => callback(status));
  },
  onBridgeLog: (callback: (log: any) => void) => {
    ipcRenderer.on('bridge-log', (_event, log) => callback(log));
  },
  checkForUpdates: () => ipcRenderer.invoke('check-for-updates'),
  getAppVersion: () => ipcRenderer.invoke('get-app-version'),
  onUpdateStatus: (callback: (status: any) => void) => {
    ipcRenderer.on('update-status', (_event, status) => callback(status));
  },
});
