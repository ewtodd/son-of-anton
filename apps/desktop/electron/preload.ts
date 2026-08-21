import { contextBridge, ipcRenderer, webFrame, webUtils } from 'electron'

// Which translucency the OS can back. Asked synchronously because the renderer
// needs it before its first paint, and answered by main because deciding it
// needs `os.release()` — a sandboxed preload may only require electron, events,
// timers and url, so importing node:os here throws before contextBridge runs
// and takes the ENTIRE bridge down with it (window.rencoDesktop undefined =>
// "Desktop IPC bridge is unavailable"). No reply means no glass, which degrades
// to an ordinary opaque window rather than a page thinned over nothing.
const translucencySupport = ipcRenderer.sendSync('renco:translucency:support')

contextBridge.exposeInMainWorld('rencoDesktop', {
  glassSupported: translucencySupport?.glass === true,
  translucencySupported: translucencySupport?.translucency === true,
  getConnection: profile => ipcRenderer.invoke('renco:connection', profile),
  // Registry-scoped backend resolution: { connectionId, profile } → descriptor.
  getConnectionFor: payload => ipcRenderer.invoke('renco:connection:for', payload),
  getProfileRoutes: profiles => ipcRenderer.invoke('renco:plugin-profile-routes', profiles),
  revalidateConnection: () => ipcRenderer.invoke('renco:connection:revalidate'),
  touchBackend: profile => ipcRenderer.invoke('renco:backend:touch', profile),
  getGatewayWsUrl: profile => ipcRenderer.invoke('renco:gateway:ws-url', profile),
  // Registry-scoped fresh WS URL: { connectionId, profile } → result shape of
  // getGatewayWsUrl, minted against that connection's backend.
  getGatewayWsUrlFor: payload => ipcRenderer.invoke('renco:gateway:ws-url-for', payload),
  // Union agent roster across every registered connection.
  getAgentRoster: () => ipcRenderer.invoke('renco:agents:roster'),
  openSessionWindow: (sessionId, opts) => ipcRenderer.invoke('renco:window:openSession', sessionId, opts),
  openSessionInTerminal: (sessionId, opts) => ipcRenderer.invoke('renco:window:openInTerminal', sessionId, opts),
  openWindow: () => ipcRenderer.invoke('renco:window:openInstance'),
  claimAmbientCue: key => ipcRenderer.invoke('renco:ambient:claim', key),
  wakeIndicator: {
    getState: () => ipcRenderer.invoke('renco:wake-indicator:get'),
    setState: state => ipcRenderer.send('renco:wake-indicator:set', state),
    onState: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('renco:wake-indicator:state', listener)

      return () => ipcRenderer.removeListener('renco:wake-indicator:state', listener)
    }
  },
  petOverlay: {
    // Main renderer → main process: window lifecycle + drag. `request` is
    // `{ bounds, screen }`; resolves with the screen bounds it actually used.
    open: request => ipcRenderer.invoke('renco:pet-overlay:open', request),
    close: () => ipcRenderer.invoke('renco:pet-overlay:close'),
    setBounds: bounds => ipcRenderer.send('renco:pet-overlay:set-bounds', bounds),
    setIgnoreMouse: ignore => ipcRenderer.send('renco:pet-overlay:ignore-mouse', ignore),
    // Flip the overlay focusable (and focus it) while the composer needs keys.
    setFocusable: focusable => ipcRenderer.send('renco:pet-overlay:set-focusable', focusable),
    // Main renderer → overlay (forwarded by main): push the latest pet state.
    pushState: payload => ipcRenderer.send('renco:pet-overlay:state', payload),
    // Overlay → main renderer (forwarded by main): pop back in / composer submit.
    control: payload => ipcRenderer.send('renco:pet-overlay:control', payload),
    // Overlay subscribes to state pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('renco:pet-overlay:state', listener)

      return () => ipcRenderer.removeListener('renco:pet-overlay:state', listener)
    },
    // Main renderer subscribes to overlay control messages.
    onControl: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('renco:pet-overlay:control', listener)

      return () => ipcRenderer.removeListener('renco:pet-overlay:control', listener)
    }
  },
  // HUD mode: the chrome-free floating chat. A full app renderer (own gateway)
  // sized as a floating bar, so it mounts the real composer. Main owns the
  // window; `onChanged` keeps every window's toggle truthful.
  hud: {
    open: request => ipcRenderer.invoke('renco:hud:open', request),
    close: () => ipcRenderer.invoke('renco:hud:close'),
    setIgnoreMouse: ignore => ipcRenderer.send('renco:hud:ignore-mouse', ignore),
    moveBy: delta => ipcRenderer.send('renco:hud:move-by', delta),
    setBounds: bounds => ipcRenderer.send('renco:hud:set-bounds', bounds),
    // Whether the band covers the window below the bar. Main pairs it with the
    // user's translucency setting to decide the native frost (macOS vibrancy /
    // Windows 11 DWM backdrop) — see hudFrostFor.
    setFrost: showing => ipcRenderer.invoke('renco:hud:frost', showing),
    // The HUD tells main which session it is on; main hands that back to the
    // app window when the HUD closes, so the app can re-home onto it.
    setSession: sessionId => ipcRenderer.send('renco:hud:session', sessionId),
    onGoto: callback => {
      const listener = (_event, sessionId) => callback(sessionId)
      ipcRenderer.on('renco:hud:goto', listener)

      return () => ipcRenderer.removeListener('renco:hud:goto', listener)
    },
    onChanged: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('renco:hud:changed', listener)

      return () => ipcRenderer.removeListener('renco:hud:changed', listener)
    },
    // Linux only, and silent elsewhere: where the cursor is, in page
    // coordinates, or null when it has left the window. Stands in for the
    // mousemove that `setIgnoreMouseEvents(true, { forward: true })` delivers on
    // macOS and Windows but not here.
    onCursor: callback => {
      const listener = (_event, point) => callback(point)
      ipcRenderer.on('renco:hud:cursor', listener)

      return () => ipcRenderer.removeListener('renco:hud:cursor', listener)
    }
  },
  // Quick Entry: the global-hotkey mini composer window. Main owns the OS
  // shortcut + the persisted preference; the quick window only captures text
  // and hands it back, and the primary renderer submits it through the normal
  // prompt path.
  quickEntry: {
    getSettings: () => ipcRenderer.invoke('renco:quick-entry:settings:get'),
    setSettings: patch => ipcRenderer.invoke('renco:quick-entry:settings:set', patch),
    submit: payload => ipcRenderer.send('renco:quick-entry:submit', payload),
    dismiss: () => ipcRenderer.send('renco:quick-entry:dismiss'),
    // Primary renderer → main → quick window: gateway connection state + the
    // recent-session options the target picker offers. Main caches the latest
    // payload so a freshly spawned quick window starts from truth.
    pushState: payload => ipcRenderer.send('renco:quick-entry:state', payload),
    // Quick window subscribes to those pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('renco:quick-entry:state', listener)

      return () => ipcRenderer.removeListener('renco:quick-entry:state', listener)
    },
    // Main → primary renderer: a submit captured by the quick window.
    onSubmit: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('renco:quick-entry:submit', listener)

      return () => ipcRenderer.removeListener('renco:quick-entry:submit', listener)
    },
    // Main → quick window: you were just summoned (reset draft + refocus).
    onShown: callback => {
      const listener = () => callback()
      ipcRenderer.on('renco:quick-entry:shown', listener)

      return () => ipcRenderer.removeListener('renco:quick-entry:shown', listener)
    }
  },
  getBootProgress: () => ipcRenderer.invoke('renco:boot-progress:get'),
  getConnectionConfig: profile => ipcRenderer.invoke('renco:connection-config:get', profile),
  saveConnectionConfig: payload => ipcRenderer.invoke('renco:connection-config:save', payload),
  applyConnectionConfig: payload => ipcRenderer.invoke('renco:connection-config:apply', payload),
  testConnectionConfig: payload => ipcRenderer.invoke('renco:connection-config:test', payload),
  // v2 multi-connection registry: named agent sources (local / remote / cloud / ssh).
  connections: {
    list: () => ipcRenderer.invoke('renco:connections:list'),
    save: payload => ipcRenderer.invoke('renco:connections:save', payload),
    remove: id => ipcRenderer.invoke('renco:connections:remove', id),
    setPrimary: id => ipcRenderer.invoke('renco:connections:set-primary', id),
    setLaunchMode: mode => ipcRenderer.invoke('renco:connections:set-launch-mode', mode),
    setLastUsed: id => ipcRenderer.invoke('renco:connections:set-last-used', id),
    test: id => ipcRenderer.invoke('renco:connections:test', id),
    // Fan out `renco update` to every eligible registered connection.
    // Optional excludeIds skips rows the caller updates through another path.
    updateAll: options => ipcRenderer.invoke('renco:connections:update-all', options),
    // Registry lifecycle push (main → renderer): a connection was removed or
    // materially edited, so secondaries scoped to it must be disposed (and,
    // for edits, re-dialed at the new target).
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('renco:connections:changed', listener)

      return () => ipcRenderer.removeListener('renco:connections:changed', listener)
    }
  },
  sshConfigHosts: () => ipcRenderer.invoke('renco:ssh-config:hosts'),
  sshResolveHost: host => ipcRenderer.invoke('renco:ssh-config:resolve', host),
  probeConnectionConfig: remoteUrl => ipcRenderer.invoke('renco:connection-config:probe', remoteUrl),
  oauthLoginConnectionConfig: remoteUrl => ipcRenderer.invoke('renco:connection-config:oauth-login', remoteUrl),
  oauthLogoutConnectionConfig: remoteUrl => ipcRenderer.invoke('renco:connection-config:oauth-logout', remoteUrl),
  // Renco Cloud: one portal login powers discovery + silent per-agent sign-in
  // (cloud-auto-discovery Phase 3).
  cloud: {
    status: () => ipcRenderer.invoke('renco:cloud:status'),
    login: () => ipcRenderer.invoke('renco:cloud:login'),
    logout: () => ipcRenderer.invoke('renco:cloud:logout'),
    discover: org => ipcRenderer.invoke('renco:cloud:discover', org),
    agentSignIn: dashboardUrl => ipcRenderer.invoke('renco:cloud:agent-sign-in', dashboardUrl)
  },
  profile: {
    get: () => ipcRenderer.invoke('renco:profile:get'),
    set: name => ipcRenderer.invoke('renco:profile:set', name)
  },
  api: request => ipcRenderer.invoke('renco:api', request),
  notify: payload => ipcRenderer.invoke('renco:notify', payload),
  requestMicrophoneAccess: () => ipcRenderer.invoke('renco:requestMicrophoneAccess'),
  readWindowBelow: () => ipcRenderer.invoke('renco:window:readBelow'),
  readFileDataUrl: filePath => ipcRenderer.invoke('renco:readFileDataUrl', filePath),
  readFileDataUrlForAttach: filePath => ipcRenderer.invoke('renco:readFileDataUrlForAttach', filePath),
  dataUrlReadMax: {
    get: () => ipcRenderer.invoke('renco:data-url-read-max:get'),
    set: maxMb => ipcRenderer.invoke('renco:data-url-read-max:set', maxMb)
  },
  readFileText: filePath => ipcRenderer.invoke('renco:readFileText', filePath),
  selectPaths: options => ipcRenderer.invoke('renco:selectPaths', options),
  selectSavePath: options => ipcRenderer.invoke('renco:selectSavePath', options),
  writeClipboard: text => ipcRenderer.invoke('renco:writeClipboard', text),
  readClipboard: () => ipcRenderer.invoke('renco:readClipboard'),
  saveGatewayFile: payload => ipcRenderer.invoke('renco:saveGatewayFile', payload),
  saveImageFromUrl: url => ipcRenderer.invoke('renco:saveImageFromUrl', url),
  contextMenuEdit: command => ipcRenderer.invoke('renco:context-menu:edit', command),
  contextMenuCopyImage: () => ipcRenderer.invoke('renco:context-menu:copy-image'),
  contextMenuSpellcheck: action => ipcRenderer.invoke('renco:context-menu:spellcheck', action),
  contextMenuGuestAddWord: payload => ipcRenderer.invoke('renco:context-menu:guest-add-word', payload),
  onContextMenuSpellcheck: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:context-menu-spellcheck', listener)

    return () => ipcRenderer.removeListener('renco:context-menu-spellcheck', listener)
  },
  saveImageBuffer: (data, ext) => ipcRenderer.invoke('renco:saveImageBuffer', { data, ext }),
  saveClipboardImage: () => ipcRenderer.invoke('renco:saveClipboardImage'),
  getPathForFile: file => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  normalizePreviewTarget: (target, baseDir) => ipcRenderer.invoke('renco:normalizePreviewTarget', target, baseDir),
  watchPreviewFile: url => ipcRenderer.invoke('renco:watchPreviewFile', url),
  watchDirectory: dir => ipcRenderer.invoke('renco:watchDirectory', dir),
  stopPreviewFileWatch: id => ipcRenderer.invoke('renco:stopPreviewFileWatch', id),
  setActiveWork: payload => ipcRenderer.send('renco:active-work', payload),
  setTitleBarTheme: payload => ipcRenderer.send('renco:titlebar-theme', payload),
  setNativeTheme: mode => ipcRenderer.send('renco:native-theme', mode),
  setTranslucency: payload => ipcRenderer.send('renco:translucency', payload),
  setKeepAwake: on => ipcRenderer.send('renco:keep-awake', on),
  setDisableF12: blocked => ipcRenderer.send('renco:devtools:disable-f12', blocked),
  setPreviewShortcutActive: active => ipcRenderer.send('renco:previewShortcutActive', Boolean(active)),
  openExternal: url => ipcRenderer.invoke('renco:openExternal', url),
  openPreviewInBrowser: url => ipcRenderer.invoke('renco:openPreviewInBrowser', url),
  reachPreviewUrl: url => ipcRenderer.invoke('renco:preview:reach', url),
  fetchLinkTitle: url => ipcRenderer.invoke('renco:fetchLinkTitle', url),
  resolveFavicon: url => ipcRenderer.invoke('renco:resolveFavicon', url),
  sanitizeWorkspaceCwd: cwd => ipcRenderer.invoke('renco:workspace:sanitize', cwd),
  settings: {
    getDefaultProjectDir: () => ipcRenderer.invoke('renco:setting:defaultProjectDir:get'),
    setDefaultProjectDir: dir => ipcRenderer.invoke('renco:setting:defaultProjectDir:set', dir),
    pickDefaultProjectDir: () => ipcRenderer.invoke('renco:setting:defaultProjectDir:pick')
  },
  zoom: {
    // Current zoom of this window, as { level, percent }.
    get: () => ipcRenderer.invoke('renco:zoom:get'),
    // Synchronous zoom factor (1 = 100%). Coordinate math needs it in the
    // same tick as the event it converts, so no IPC round-trip here.
    factor: () => webFrame.getZoomFactor(),
    setPercent: percent => ipcRenderer.send('renco:zoom:set-percent', percent),
    // Fires on every zoom change, including the Ctrl/Cmd +/-/0 shortcuts,
    // so the settings UI can stay in sync with the keyboard.
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('renco:zoom:changed', listener)

      return () => ipcRenderer.removeListener('renco:zoom:changed', listener)
    }
  },
  revealLogs: () => ipcRenderer.invoke('renco:logs:reveal'),
  getRecentLogs: () => ipcRenderer.invoke('renco:logs:recent'),
  // Fire-and-forget: persists a renderer error-boundary catch (with component
  // stack) to desktop.log so crashes survive the window (#79428).
  reportRendererError: report => ipcRenderer.send('renco:logs:renderer-error', report),
  readDir: dirPath => ipcRenderer.invoke('renco:fs:readDir', dirPath),
  gitRoot: startPath => ipcRenderer.invoke('renco:fs:gitRoot', startPath),
  revealPath: targetPath => ipcRenderer.invoke('renco:fs:reveal', targetPath),
  openDir: dirPath => ipcRenderer.invoke('renco:fs:openDir', dirPath),
  desktopPluginsRoot: () => ipcRenderer.invoke('renco:fs:desktopPluginsRoot'),
  agentPluginsRoot: () => ipcRenderer.invoke('renco:fs:agentPluginsRoot'),
  renamePath: (targetPath, newName) => ipcRenderer.invoke('renco:fs:rename', targetPath, newName),
  writeTextFile: (filePath, content) => ipcRenderer.invoke('renco:fs:writeText', filePath, content),
  trashPath: targetPath => ipcRenderer.invoke('renco:fs:trash', targetPath),
  git: {
    worktreeList: repoPath => ipcRenderer.invoke('renco:git:worktreeList', repoPath),
    worktreeAdd: (repoPath, options) => ipcRenderer.invoke('renco:git:worktreeAdd', repoPath, options),
    worktreeRemove: (repoPath, worktreePath, options) =>
      ipcRenderer.invoke('renco:git:worktreeRemove', repoPath, worktreePath, options),
    branchSwitch: (repoPath, branch) => ipcRenderer.invoke('renco:git:branchSwitch', repoPath, branch),
    branchList: repoPath => ipcRenderer.invoke('renco:git:branchList', repoPath),
    baseBranchList: repoPath => ipcRenderer.invoke('renco:git:baseBranchList', repoPath),
    repoStatus: repoPath => ipcRenderer.invoke('renco:git:repoStatus', repoPath),
    fileDiff: (repoPath, filePath) => ipcRenderer.invoke('renco:git:fileDiff', repoPath, filePath),
    scanRepos: (roots, options) => ipcRenderer.invoke('renco:git:scanRepos', roots, options),
    review: {
      list: (repoPath, scope, baseRef) => ipcRenderer.invoke('renco:git:review:list', repoPath, scope, baseRef),
      diff: (repoPath, filePath, scope, baseRef, staged) =>
        ipcRenderer.invoke('renco:git:review:diff', repoPath, filePath, scope, baseRef, staged),
      stage: (repoPath, filePath) => ipcRenderer.invoke('renco:git:review:stage', repoPath, filePath),
      unstage: (repoPath, filePath) => ipcRenderer.invoke('renco:git:review:unstage', repoPath, filePath),
      revert: (repoPath, filePath) => ipcRenderer.invoke('renco:git:review:revert', repoPath, filePath),
      revParse: (repoPath, ref) => ipcRenderer.invoke('renco:git:review:revParse', repoPath, ref),
      commit: (repoPath, message, push) => ipcRenderer.invoke('renco:git:review:commit', repoPath, message, push),
      commitContext: repoPath => ipcRenderer.invoke('renco:git:review:commitContext', repoPath),
      push: repoPath => ipcRenderer.invoke('renco:git:review:push', repoPath),
      shipInfo: repoPath => ipcRenderer.invoke('renco:git:review:shipInfo', repoPath),
      prList: (repoPath, branches, numbers) =>
        ipcRenderer.invoke('renco:git:review:prList', repoPath, branches, numbers),
      fetchPrComment: (repoPath, url) => ipcRenderer.invoke('renco:git:review:fetchPrComment', repoPath, url),
      createPr: repoPath => ipcRenderer.invoke('renco:git:review:createPr', repoPath)
    }
  },
  terminal: {
    cwd: id => ipcRenderer.invoke('renco:terminal:cwd', id),
    dispose: id => ipcRenderer.invoke('renco:terminal:dispose', id),
    resize: (id, size) => ipcRenderer.invoke('renco:terminal:resize', id, size),
    start: options => ipcRenderer.invoke('renco:terminal:start', options),
    write: (id, data) => ipcRenderer.invoke('renco:terminal:write', id, data),
    onData: (id, callback) => {
      const channel = `renco:terminal:${id}:data`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    },
    onExit: (id, callback) => {
      const channel = `renco:terminal:${id}:exit`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    }
  },
  onClosePreviewRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('renco:close-preview-requested', listener)

    return () => ipcRenderer.removeListener('renco:close-preview-requested', listener)
  },
  onPreviewNav: callback => {
    const listener = (_event, command) => callback(command)
    ipcRenderer.on('renco:preview-nav', listener)

    return () => ipcRenderer.removeListener('renco:preview-nav', listener)
  },
  onOpenFolderRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('renco:open-folder-requested', listener)

    return () => ipcRenderer.removeListener('renco:open-folder-requested', listener)
  },
  onOpenUpdatesRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('renco:open-updates', listener)

    return () => ipcRenderer.removeListener('renco:open-updates', listener)
  },
  onDeepLink: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:deep-link', listener)

    return () => ipcRenderer.removeListener('renco:deep-link', listener)
  },
  signalDeepLinkReady: () => ipcRenderer.invoke('renco:deep-link-ready'),
  probePluginRepo: payload => ipcRenderer.invoke('renco:plugin:probe', payload),
  installDesktopPlugin: payload => ipcRenderer.invoke('renco:plugin:installDesktop', payload),
  onWindowStateChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:window-state-changed', listener)

    return () => ipcRenderer.removeListener('renco:window-state-changed', listener)
  },
  onFocusSession: callback => {
    const listener = (_event, sessionId) => callback(sessionId)
    ipcRenderer.on('renco:focus-session', listener)

    return () => ipcRenderer.removeListener('renco:focus-session', listener)
  },
  onNotificationAction: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:notification-action', listener)

    return () => ipcRenderer.removeListener('renco:notification-action', listener)
  },
  onNotificationActivate: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:notification-activate', listener)

    return () => ipcRenderer.removeListener('renco:notification-activate', listener)
  },
  onPreviewFileChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:preview-file-changed', listener)

    return () => ipcRenderer.removeListener('renco:preview-file-changed', listener)
  },
  onBackendExit: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:backend-exit', listener)

    return () => ipcRenderer.removeListener('renco:backend-exit', listener)
  },
  // Soft gateway-mode apply finished tearing down the primary backend. Renderer
  // should wipe session lists + re-dial without a window reload.
  onConnectionApplied: callback => {
    const listener = () => callback()
    ipcRenderer.on('renco:connection:applied', listener)

    return () => ipcRenderer.removeListener('renco:connection:applied', listener)
  },
  onPowerResume: callback => {
    const listener = () => callback()
    ipcRenderer.on('renco:power-resume', listener)

    return () => ipcRenderer.removeListener('renco:power-resume', listener)
  },
  // AC ↔ battery transitions; renderers slow their backstop polls on battery.
  getOnBattery: () => ipcRenderer.invoke('renco:power-battery:get'),
  onBatteryChanged: callback => {
    const listener = (_event, onBattery) => callback(Boolean(onBattery))
    ipcRenderer.on('renco:power-battery', listener)

    return () => ipcRenderer.removeListener('renco:power-battery', listener)
  },
  onBootProgress: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:boot-progress', listener)

    return () => ipcRenderer.removeListener('renco:boot-progress', listener)
  },
  // First-launch bootstrap progress -- emitted by the install.ps1 stage
  // runner in main.ts (apps/desktop/electron/bootstrap-runner.ts).
  // Renderer's install overlay subscribes to live events and queries the
  // current snapshot via getBootstrapState() to recover after a devtools
  // reload mid-bootstrap.
  getBootstrapState: () => ipcRenderer.invoke('renco:bootstrap:get'),
  continueBootstrapLocal: () => ipcRenderer.invoke('renco:bootstrap:continue-local'),
  resetBootstrap: () => ipcRenderer.invoke('renco:bootstrap:reset'),
  repairBootstrap: () => ipcRenderer.invoke('renco:bootstrap:repair'),
  cancelBootstrap: () => ipcRenderer.invoke('renco:bootstrap:cancel'),
  onBootstrapEvent: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('renco:bootstrap:event', listener)

    return () => ipcRenderer.removeListener('renco:bootstrap:event', listener)
  },
  getVersion: () => ipcRenderer.invoke('renco:version'),
  getRemoteDisplayReason: () => ipcRenderer.invoke('renco:get-remote-display-reason'),
  uninstall: {
    summary: () => ipcRenderer.invoke('renco:uninstall:summary'),
    run: mode => ipcRenderer.invoke('renco:uninstall:run', { mode })
  },
  updates: {
    check: () => ipcRenderer.invoke('renco:updates:check'),
    apply: opts => ipcRenderer.invoke('renco:updates:apply', opts),
    getBranch: () => ipcRenderer.invoke('renco:updates:branch:get'),
    setBranch: name => ipcRenderer.invoke('renco:updates:branch:set', name),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('renco:updates:progress', listener)

      return () => ipcRenderer.removeListener('renco:updates:progress', listener)
    }
  },
  themes: {
    fetchMarketplace: id => ipcRenderer.invoke('renco:vscode-theme:fetch', id),
    searchMarketplace: query => ipcRenderer.invoke('renco:vscode-theme:search', query)
  },
  // Find-in-page (Ctrl/Cmd+F): delegates to Electron's
  // webContents.findInPage on the IPC sender's window so a Cmd+F pressed
  // in a secondary session window searches THAT window, not the primary.
  // `onFoundInPage` returns the unsubscribe fn; the renderer wires it via
  // `initFindInPageListener` in store/find-in-page.ts and tears it down
  // when the FindBar unmounts.
  findInPage: (query, options) => ipcRenderer.invoke('renco:find-in-page', query, options),
  stopFindInPage: () => ipcRenderer.invoke('renco:stop-find-in-page'),
  onFoundInPage: callback => {
    const listener = (_event, result) => callback(result)
    ipcRenderer.on('renco:found-in-page', listener)

    return () => ipcRenderer.removeListener('renco:found-in-page', listener)
  },
  // Main-process `before-input-event` forwards Ctrl/Cmd+F here so renderer
  // can open the FindBar even when the GTK compositor has already grabbed
  // the chord at the windowing layer (#81727).
  onOpenFindBarRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('renco:open-find-bar', listener)

    return () => ipcRenderer.removeListener('renco:open-find-bar', listener)
  }
})
