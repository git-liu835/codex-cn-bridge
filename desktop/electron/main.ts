import { app, BrowserWindow, Tray, Menu, nativeImage, ipcMain, dialog, shell } from 'electron';
import { spawn, execSync, ChildProcess } from 'child_process';
import path from 'path';
import fs from 'fs';

// electron-updater 可选加载（避免模块缺失导致崩溃）
let autoUpdater: any = null;
try { autoUpdater = require('electron-updater').autoUpdater; } catch { /* optional */ }

// 禁用自动下载，由用户决定是否更新
if (autoUpdater) {
  autoUpdater.autoDownload = false;
  autoUpdater.allowDowngrade = false;
}

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let bridgeProcess: ChildProcess | null = null;
let isQuitting = false;
let bridgeRestartCount = 0;
const MAX_RESTART = 5;

const BRIDGE_PORT = 8765;
const isDev = !app.isPackaged;

// ── 单实例锁（防止多个托盘图标）──────────────────────────────────────
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    // 用户再次启动时，激活已有窗口而不是创建新实例
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });

  // ── 以下所有初始化仅在主实例中执行 ───────────────────────────────

function findConfigPath(): string | null {
  // 优先找项目根目录的 config.yaml（适合开发模式）
  const projectConfig = path.join(__dirname, '..', '..', 'config.yaml');
  if (fs.existsSync(projectConfig)) return projectConfig;
  // 其次找用户主目录的配置
  const homeConfig = path.join(app.getPath('home'), '.code-cn-bridge.yaml');
  if (fs.existsSync(homeConfig)) return homeConfig;
  return null;
}

function getBridgeCommand(): { cmd: string; args: string[] } {
  const configPath = findConfigPath();
  const configArgs: string[] = configPath ? ['-c', configPath] : [];

  if (isDev) {
    return {
      cmd: 'python',
      args: ['-m', 'code_cn_bridge.cli', 'start', '--port', String(BRIDGE_PORT), ...configArgs],
    };
  }
  // 生产模式：使用 PyInstaller 打包的可执行文件
  const exeName = process.platform === 'win32' ? 'code-cn-bridge.exe' : 'code-cn-bridge';
  const exePath = path.join(process.resourcesPath, 'backend', exeName);
  if (fs.existsSync(exePath)) {
    return { cmd: exePath, args: ['start', '--port', String(BRIDGE_PORT), ...configArgs] };
  }
  // 回退到 python 模块
  return {
    cmd: 'python',
    args: ['-m', 'code_cn_bridge.cli', 'start', '--port', String(BRIDGE_PORT), ...configArgs],
  };
}

function startBridgeProcess() {
  if (bridgeProcess) return;

  // 强制释放端口（解决上次进程残留导致的端口占用循环）
  try {
    if (process.platform === 'win32') {
      const psScript = `
        $conns = Get-NetTCPConnection -LocalPort ${BRIDGE_PORT} -EA SilentlyContinue |
          Where-Object { $_.OwningProcess -gt 0 }
        foreach ($c in $conns) {
          Stop-Process -Id $c.OwningProcess -Force -EA SilentlyContinue
        }
      `;
      execSync(`powershell -NoProfile -Command "${psScript.replace(/\n/g, ' ')}"`, { timeout: 5000 });
    } else {
      execSync(`lsof -ti:${BRIDGE_PORT} | xargs kill -9 2>/dev/null; true`, { timeout: 5000 });
    }
  } catch { /* ignore */ }

  const { cmd, args } = getBridgeCommand();
  console.log(`[Main] Starting bridge: ${cmd} ${args.join(' ')}`);

  bridgeProcess = spawn(cmd, args, {
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' },
  });

  bridgeProcess.stdout?.on('data', (data: Buffer) => {
    const text = data.toString().trim();
    try {
      console.log(`[Bridge] ${text}`);
    } catch { /* EPIPE when parent pipe closed */ }
    try { mainWindow?.webContents.send('bridge-log', { level: 'info', text }); } catch { /* window gone */ }
  });

  bridgeProcess.stderr?.on('data', (data: Buffer) => {
    const text = data.toString().trim();
    try {
      console.error(`[Bridge Error] ${text}`);
    } catch { /* EPIPE when parent pipe closed */ }
    try { mainWindow?.webContents.send('bridge-log', { level: 'error', text }); } catch { /* window gone */ }
  });

  bridgeProcess.on('close', (code: number | null) => {
    console.log(`[Main] Bridge process exited with code ${code}`);
    bridgeProcess = null;
    mainWindow?.webContents.send('bridge-status', { running: false });

    // 自动重启（非用户主动退出，最多重试 MAX_RESTART 次）
    if (!isQuitting && code !== 0 && bridgeRestartCount < MAX_RESTART) {
      bridgeRestartCount++;
      console.log(`[Main] Auto-restarting bridge in 3s (attempt ${bridgeRestartCount}/${MAX_RESTART})...`);
      setTimeout(startBridgeProcess, 3000);
    } else if (bridgeRestartCount >= MAX_RESTART) {
      console.error(`[Main] Bridge failed after ${MAX_RESTART} retries, giving up.`);
    }
  });

  bridgeProcess.on('error', (err: Error) => {
    console.error('[Main] Failed to start bridge:', err.message);
    bridgeProcess = null;
  });

  // 成功运行后重置计数
  setTimeout(() => { bridgeRestartCount = 0; }, 5000);
  mainWindow?.webContents.send('bridge-status', { running: true });
}

function stopBridgeProcess() {
  if (!bridgeProcess) return;

  // 尝试优雅关闭
  try {
    const http = require('http');
    const req = http.request({
      hostname: '127.0.0.1',
      port: BRIDGE_PORT,
      path: '/admin/api/shutdown',
      method: 'POST',
      timeout: 3000,
    });
    req.on('error', () => {});
    req.end();
  } catch {
    // ignore
  }

  setTimeout(() => {
    if (bridgeProcess) {
      bridgeProcess.kill('SIGTERM');
      bridgeProcess = null;
    }
  }, 1500);
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 720,
    minWidth: 900,
    minHeight: 600,
    title: 'code CN Bridge',
    backgroundColor: '#0f1117',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
    frame: true,
    titleBarStyle: 'default',
  });

  // 窗口准备好后显示
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
  });

  // 关闭窗口 → 根据设置决定隐藏到托盘或退出
  mainWindow.on('close', (event) => {
    if (!isQuitting && getCloseToTraySetting()) {
      event.preventDefault();
      mainWindow?.hide();
    }
  });

  if (isDev) {
    mainWindow.loadURL('http://localhost:5173');
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  } else {
    mainWindow.loadFile(path.join(__dirname, '../dist/index.html'));
  }

  mainWindow.on('closed', () => {
    mainWindow = null;
  });
}

function createTray() {
  // 加载真实的图标文件（而非 createEmpty 导致的空白图标）
  let icon = nativeImage.createEmpty();
  try {
    const iconPath = isDev
      ? path.join(__dirname, '..', 'assets', 'icon.png')
      : path.join(process.resourcesPath, 'icon.png');
    if (fs.existsSync(iconPath)) {
      icon = nativeImage.createFromPath(iconPath);
      // 托盘图标大小：Windows 16x16, macOS 22x22 (2x retina)
      const traySize = process.platform === 'darwin' ? 44 : 16;
      icon = icon.resize({ width: traySize, height: traySize });
    }
  } catch {
    // 加载失败则保持空图标（至少不会崩溃）
  }
  tray = new Tray(icon);

  // 使用自定义标题
  if (process.platform === 'darwin') {
    tray.setTitle('CN');
  }

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示主窗口',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        } else {
          createWindow();
        }
      },
    },
    { type: 'separator' },
    {
      label: '停止代理',
      click: () => {
        stopBridgeProcess();
      },
    },
    {
      label: '启动代理',
      click: () => {
        startBridgeProcess();
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        isQuitting = true;
        stopBridgeProcess();
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);
  tray.setToolTip('code CN Bridge');

  tray.on('double-click', () => {
    if (mainWindow) {
      mainWindow.show();
      mainWindow.focus();
    } else {
      createWindow();
    }
  });
}

// ── IPC Handlers ────────────────────────────────────────────────────

ipcMain.handle('get-bridge-status', async () => {
  try {
    const http = require('http');
    return new Promise((resolve) => {
      const req = http.get(`http://127.0.0.1:${BRIDGE_PORT}/admin/api/status`, (res: any) => {
        let body = '';
        res.on('data', (chunk: string) => { body += chunk; });
        res.on('end', () => {
          try { resolve(JSON.parse(body)); } catch { resolve({ running: false }); }
        });
      });
      req.on('error', () => resolve({ running: false }));
      req.setTimeout(3000, () => { req.destroy(); resolve({ running: false }); });
    });
  } catch {
    return { running: false };
  }
});

ipcMain.handle('export-config', async () => {
  if (!mainWindow) return { yaml: '' };
  try {
    return await mainWindow.webContents.executeJavaScript(
      `fetch('http://127.0.0.1:${BRIDGE_PORT}/admin/api/config/export').then(r => r.json())`
    );
  } catch {
    return { yaml: '' };
  }
});

ipcMain.handle('import-config', async (_event, yamlStr: string) => {
  try {
    const http = require('http');
    const data = JSON.stringify({ yaml: yamlStr });
    return new Promise((resolve) => {
      const req = http.request({
        hostname: '127.0.0.1', port: BRIDGE_PORT,
        path: '/admin/api/config/import', method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Content-Length': data.length },
      }, (res: any) => {
        let body = '';
        res.on('data', (chunk: string) => { body += chunk; });
        res.on('end', () => resolve(JSON.parse(body)));
      });
      req.on('error', () => resolve({ error: '连接失败' }));
      req.write(data);
      req.end();
    });
  } catch {
    return { error: '导入失败' };
  }
});

ipcMain.handle('select-file', async (_event, options: { filters?: Array<{ name: string; extensions: string[] }> }) => {
  const result = await dialog.showOpenDialog(mainWindow!, {
    properties: ['openFile'],
    filters: options.filters || [{ name: 'YAML', extensions: ['yaml', 'yml'] }],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('save-file', async (_event, options: { defaultPath?: string; content: string }) => {
  const result = await dialog.showSaveDialog(mainWindow!, {
    defaultPath: options.defaultPath || 'config.yaml',
    filters: [{ name: 'YAML', extensions: ['yaml', 'yml'] }],
  });
  if (!result.canceled && result.filePath) {
    fs.writeFileSync(result.filePath, options.content, 'utf-8');
    return result.filePath;
  }
  return null;
});

ipcMain.handle('open-external', async (_event, url: string) => {
  await shell.openExternal(url);
});

ipcMain.handle('check-for-updates', async () => {
  if (isDev) return { status: 'dev-mode' };
  if (!autoUpdater) return { status: 'error', message: 'electron-updater 模块不可用' };
  try {
    const mirror = getUpdateMirror();
    if (mirror) {
      const ok = await configureMirror(mirror);
      if (!ok) {
        return { status: 'error', message: '镜像连接失败，请检查镜像地址' };
      }
    }
    const result = await autoUpdater.checkForUpdates();
    return { status: 'ok', version: result?.updateInfo?.version };
  } catch (err: any) {
    const detail = [err.message || String(err)];
    if (err.stack) detail.push('Stack: ' + err.stack);
    if (detail[0].includes('TIMED_OUT') || detail[0].includes('CONNECTION')) {
      detail.push('提示：如在中国大陆，请设置更新镜像 (ghproxy.com)');
    }
    return { status: 'error', message: detail.join('\n') };
  }
});

ipcMain.handle('get-app-version', async () => {
  return app.getVersion();
});

// ── 更新镜像 ──────────────────────────────────────────────────────

function getUpdateMirror(): string {
  const configPath = findConfigPath();
  if (!configPath) return '';
  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    const match = content.match(/^\s*update_mirror:\s*["']?([^"'\n\r]+)["']?\s*$/m);
    if (match) return match[1].trim();
  } catch { /* ignore */ }
  return '';
}

async function configureMirror(mirror: string): Promise<boolean> {
  const https = require('https');
  const apiUrl = mirror.replace(/\/?$/, '') +
    '/https://api.github.com/repos/git-liu835/codex-cn-bridge/releases/latest';

  return new Promise((resolve) => {
    const req = https.get(apiUrl, { timeout: 30000 }, (res: any) => {
      let body = '';
      res.on('data', (chunk: string) => { body += chunk; });
      res.on('end', () => {
        try {
          const release = JSON.parse(body);
          const tag: string = release.tag_name;
          const baseUrl = mirror.replace(/\/?$/, '') +
            '/https://github.com/git-liu835/codex-cn-bridge/releases/download/' + tag;
          console.log('[AutoUpdater] Mirror configured:', baseUrl);
          // @ts-ignore — generic provider URL
          autoUpdater.setFeedURL({ provider: 'generic', url: baseUrl });
          resolve(true);
        } catch (e: any) {
          console.error('[AutoUpdater] Mirror parse failed:', e.message);
          resolve(false);
        }
      });
    });
    req.on('error', (e: any) => {
      console.error('[AutoUpdater] Mirror fetch failed:', e.message);
      resolve(false);
    });
    req.setTimeout(30000, () => { req.destroy(); resolve(false); });
  });
}

// ── 自动更新 ───────────────────────────────────────────────────────

function setupAutoUpdater() {
  if (isDev) {
    console.log('[AutoUpdater] Skipping update check in dev mode');
    return;
  }
  if (!autoUpdater) {
    console.log('[AutoUpdater] electron-updater not available, skipping');
    return;
  }

  autoUpdater.on('update-available', (info: any) => {
    console.log('[AutoUpdater] Update available:', info.version);
    dialog.showMessageBox(mainWindow!, {
      type: 'info',
      title: '发现新版本',
      message: `新版本 v${info.version} 可用`,
      detail: `当前版本: v${app.getVersion()}\n\n是否立即下载更新？`,
      buttons: ['立即更新', '稍后提醒'],
      defaultId: 0,
      cancelId: 1,
    }).then(({ response }) => {
      if (response === 0) {
        autoUpdater.downloadUpdate();
        mainWindow?.webContents.send('update-status', { status: 'downloading' });
      }
    });
  });

  autoUpdater.on('download-progress', (progress: any) => {
    mainWindow?.webContents.send('update-status', {
      status: 'downloading',
      percent: Math.floor(progress.percent),
    });
  });

  autoUpdater.on('update-downloaded', () => {
    mainWindow?.webContents.send('update-status', { status: 'downloaded' });
    dialog.showMessageBox(mainWindow!, {
      type: 'info',
      title: '更新已下载',
      message: '更新已下载完成，是否立即退出并安装？',
      detail: '安装完成后程序将自动重启。',
      buttons: ['退出并安装', '稍后安装'],
      defaultId: 0,
      cancelId: 1,
    }).then(({ response }) => {
      if (response === 0) {
        autoUpdater.quitAndInstall();
      }
    });
  });

  autoUpdater.on('update-not-available', () => {
    mainWindow?.webContents.send('update-status', { status: 'up-to-date' });
  });

  autoUpdater.on('error', (err: any) => {
    console.error('[AutoUpdater] Error:', err.message);
    const detail = [err.message];
    if ((err as any).stack) detail.push('Stack: ' + (err as any).stack);
    mainWindow?.webContents.send('update-status', { status: 'error', message: detail.join('\n') });
  });

  // 延迟检查，让应用先启动完成
  setTimeout(async () => {
    try {
      const mirror = getUpdateMirror();
      if (mirror) {
        const ok = await configureMirror(mirror);
        if (!ok) {
          mainWindow?.webContents.send('update-status', {
            status: 'error',
            message: `镜像连接失败: ${mirror}\n请检查镜像地址是否正确，或清除镜像后重试。`,
          });
          return;
        }
      }
      await autoUpdater.checkForUpdates();
    } catch (err: any) {
      console.error('[AutoUpdater] Check failed:', err.message);
      const detail = [err.message];
      if ((err as any).stack) detail.push('Stack: ' + (err as any).stack);
      if (detail[0].includes('TIMED_OUT') || detail[0].includes('CONNECTION')) {
        detail.push('\n提示：如在中国大陆，请设置更新镜像 (ghproxy.com)');
      }
      mainWindow?.webContents.send('update-status', { status: 'error', message: detail.join('\n') });
    }
  }, 5000);
}

// ── App Lifecycle ────────────────────────────────────────────────────

function getCloseToTraySetting(): boolean {
  const configPath = findConfigPath();
  if (!configPath) return true;
  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    const match = content.match(/^\s*close_to_tray:\s*(true|false)\s*$/m);
    if (match) return match[1] === 'true';
  } catch { /* ignore */ }
  return true;
}

function getAutoStartSetting(): boolean {
  const configPath = findConfigPath();
  if (!configPath) return true;
  try {
    const content = fs.readFileSync(configPath, 'utf-8');
    const match = content.match(/^\s*auto_start:\s*(true|false)\s*$/m);
    if (match) return match[1] === 'true';
  } catch { /* ignore */ }
  return true;
}

// ── 维护注册表 InstallLocation ──────────────────────────────────────
// NSIS 静默安装时通过注册表查找原始安装路径。
// 如果注册表键缺失或 InstallLocation 为空，更新时会装到默认路径而非用户选择的位置。

function ensureRegistryKey() {
  if (process.platform !== 'win32') return;
  try {
    const currentDir = path.dirname(app.getPath('exe'));
    const displayVersion = app.getVersion();
    // GUID 由 electron-builder 根据 appId (com.code-cn-bridge.app) 生成，固定不变
    const guid = '{69bf6637-f80f-5ed3-a42e-764c81b1e2f2}';
    const esc = (s: string) => s.replace(/'/g, "''");

    // 先读取注册表中已有的 InstallLocation，避免用临时目录覆盖真实安装路径
    const keyPath = `HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\${guid}`;
    let installDir = currentDir;
    try {
      const existingLoc = execSync(
        `powershell -NoProfile -Command "(Get-ItemProperty -Path '${keyPath}' -Name InstallLocation -EA SilentlyContinue).InstallLocation"`,
        { timeout: 5000, stdio: 'pipe' }
      ).toString().trim();
      if (existingLoc && !existingLoc.includes('\\Temp\\') && !existingLoc.includes('\\temp\\')) {
        installDir = existingLoc;
      }
    } catch {
      // 注册表项不存在，使用当前路径
    }

    const psLines = [
      `$p = '${keyPath}'`,
      `New-Item -Path $p -Force | Out-Null`,
      `Set-ItemProperty -Path $p -Name DisplayName -Value 'code CN Bridge' -Type String -Force`,
      `Set-ItemProperty -Path $p -Name DisplayVersion -Value '${esc(displayVersion)}' -Type String -Force`,
      `Set-ItemProperty -Path $p -Name InstallLocation -Value '${esc(installDir)}' -Type String -Force`,
      `Set-ItemProperty -Path $p -Name UninstallString -Value '${esc(installDir)}\\Uninstall code CN Bridge.exe' -Type String -Force`,
      `Set-ItemProperty -Path $p -Name Publisher -Value 'git-liu835' -Type String -Force`,
      `Set-ItemProperty -Path $p -Name DisplayIcon -Value '${esc(installDir)}\\code CN Bridge.exe' -Type String -Force`,
    ];
    const psScript = psLines.join('; ');
    execSync(`powershell -NoProfile -Command "${psScript}"`, { timeout: 10000, stdio: 'pipe' });
    console.log(`[Registry] InstallLocation maintained: ${installDir}`);
  } catch (err: any) {
    console.error('[Registry] Failed to maintain registry key:', err.message);
  }
}

app.whenReady().then(() => {
  createTray();
  createWindow();
  ensureRegistryKey();
  setupAutoUpdater();
  if (getAutoStartSetting()) {
    startBridgeProcess();
  } else {
    console.log('[Main] auto_start disabled, bridge not started automatically');
  }
});

app.on('window-all-closed', () => {
  // 不退出，保持托盘运行
});

app.on('activate', () => {
  if (mainWindow) {
    mainWindow.show();
  } else {
    createWindow();
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  stopBridgeProcess();
  // 销毁托盘图标，防止残留空白图标
  if (tray) {
    tray.destroy();
    tray = null;
  }
});

app.on('quit', () => {
  stopBridgeProcess();
  if (tray) {
    tray.destroy();
    tray = null;
  }
});

} // else (gotTheLock) — 主实例初始化结束
