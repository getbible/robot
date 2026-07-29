const LAUNCH_TOKEN_PATTERN = /^[A-Za-z0-9_-]{1,256}$/;
const FULLSCREEN_API_VERSION = "8.0";
const INSET_SIDES = Object.freeze(["top", "right", "bottom", "left"]);
const MAX_SAFE_AREA_INSET = 320;

export class TelegramBridge {
  #webApp;
  #backHandler = null;
  #contentSafeAreaHandler;
  #fullscreenChangedHandler;
  #fullscreenFailedHandler;
  #safeAreaHandler;
  #themeHandler;
  #viewportHandler;

  constructor(webApp = window.Telegram?.WebApp ?? null) {
    this.#webApp = webApp;
    this.#themeHandler = () => this.applyTheme();
    this.#viewportHandler = () => this.applyViewport();
    this.#safeAreaHandler = () => this.applySafeAreas();
    this.#contentSafeAreaHandler = () => this.applySafeAreas();
    this.#fullscreenChangedHandler = () => {
      this.applyViewport();
      this.applySafeAreas();
    };
    this.#fullscreenFailedHandler = () => {
      this.applyViewport();
      this.applySafeAreas();
    };
  }

  get available() {
    return Boolean(
      this.#webApp &&
      typeof this.#webApp.initData === "string" &&
      this.#webApp.initData.length > 0,
    );
  }

  get initData() {
    return this.available ? this.#webApp.initData : "";
  }

  get launchToken() {
    const url = new URL(window.location.href);
    const queryToken =
      url.searchParams.get("tgWebAppStartParam") ??
      url.searchParams.get("startapp") ??
      url.searchParams.get("launch");
    const hashParams = new URLSearchParams(url.hash.replace(/^#/, ""));
    const hashToken = hashParams.get("startapp") ?? hashParams.get("launch");
    const telegramToken = this.#webApp?.initDataUnsafe?.start_param;
    return validLaunchToken(queryToken) ??
      validLaunchToken(hashToken) ??
      validLaunchToken(telegramToken);
  }

  initialize() {
    if (!this.available) {
      return false;
    }
    this.applyTheme();
    this.applyViewport();
    this.applySafeAreas();
    this.#webApp.onEvent?.("themeChanged", this.#themeHandler);
    this.#webApp.onEvent?.("viewportChanged", this.#viewportHandler);
    this.#webApp.onEvent?.("safeAreaChanged", this.#safeAreaHandler);
    this.#webApp.onEvent?.(
      "contentSafeAreaChanged",
      this.#contentSafeAreaHandler,
    );
    this.#webApp.onEvent?.(
      "fullscreenChanged",
      this.#fullscreenChangedHandler,
    );
    this.#webApp.onEvent?.(
      "fullscreenFailed",
      this.#fullscreenFailedHandler,
    );
    this.#webApp.expand?.();
    this.#webApp.enableVerticalSwipes?.();
    this.#webApp.setHeaderColor?.("bg_color");
    this.#webApp.setBackgroundColor?.("bg_color");
    this.#webApp.setBottomBarColor?.("secondary_bg_color");
    this.#webApp.ready?.();
    this.requestFullscreen();
    return true;
  }

  applyTheme() {
    const scheme = this.#webApp?.colorScheme === "dark" ? "dark" : "light";
    document.documentElement.dataset.theme = scheme;
  }

  applyViewport() {
    const stableHeight = Number(this.#webApp?.viewportStableHeight);
    if (Number.isFinite(stableHeight) && stableHeight >= 320) {
      document.documentElement.style.setProperty(
        "--app-height",
        `${stableHeight}px`,
      );
    }
  }

  applySafeAreas() {
    const root = document.documentElement;
    applyInsetVariables(
      root,
      "--bridge-safe-area-inset",
      this.#webApp?.safeAreaInset,
    );
    applyInsetVariables(
      root,
      "--bridge-content-safe-area-inset",
      this.#webApp?.contentSafeAreaInset,
    );
  }

  requestFullscreen() {
    if (
      this.#webApp?.isFullscreen ||
      typeof this.#webApp?.requestFullscreen !== "function" ||
      !supportsVersion(this.#webApp, FULLSCREEN_API_VERSION)
    ) {
      return false;
    }
    try {
      this.#webApp.requestFullscreen();
      return true;
    } catch {
      this.applyViewport();
      this.applySafeAreas();
      return false;
    }
  }

  setBackAction(handler) {
    if (this.#backHandler) {
      this.#webApp?.BackButton?.offClick?.(this.#backHandler);
    }
    this.#backHandler = typeof handler === "function" ? handler : null;
    if (this.#backHandler) {
      this.#webApp?.BackButton?.onClick?.(this.#backHandler);
      this.#webApp?.BackButton?.show?.();
    } else {
      this.#webApp?.BackButton?.hide?.();
    }
  }

  setClosingConfirmation(enabled) {
    if (enabled) {
      this.#webApp?.enableClosingConfirmation?.();
    } else {
      this.#webApp?.disableClosingConfirmation?.();
    }
  }

  notifySelection() {
    this.#webApp?.HapticFeedback?.selectionChanged?.();
  }

  notifySuccess() {
    this.#webApp?.HapticFeedback?.notificationOccurred?.("success");
  }

  notifyError() {
    this.#webApp?.HapticFeedback?.notificationOccurred?.("error");
  }

  confirm(message) {
    if (typeof this.#webApp?.showConfirm === "function") {
      return new Promise((resolve) => this.#webApp.showConfirm(message, resolve));
    }
    return Promise.resolve(window.confirm(message));
  }

  dismissKeyboard() {
    const activeElement = document.activeElement;
    if (
      activeElement &&
      typeof activeElement.matches === "function" &&
      activeElement.matches(
        'input, textarea, select, [contenteditable="true"]',
      ) &&
      typeof activeElement.blur === "function"
    ) {
      activeElement.blur();
    }
  }

  close() {
    this.#webApp?.close?.();
  }

  destroy() {
    this.setBackAction(null);
    this.#webApp?.offEvent?.("themeChanged", this.#themeHandler);
    this.#webApp?.offEvent?.("viewportChanged", this.#viewportHandler);
    this.#webApp?.offEvent?.("safeAreaChanged", this.#safeAreaHandler);
    this.#webApp?.offEvent?.(
      "contentSafeAreaChanged",
      this.#contentSafeAreaHandler,
    );
    this.#webApp?.offEvent?.(
      "fullscreenChanged",
      this.#fullscreenChangedHandler,
    );
    this.#webApp?.offEvent?.(
      "fullscreenFailed",
      this.#fullscreenFailedHandler,
    );
  }
}

export function validLaunchToken(value) {
  return typeof value === "string" && LAUNCH_TOKEN_PATTERN.test(value)
    ? value
    : null;
}

function applyInsetVariables(root, prefix, inset) {
  for (const side of INSET_SIDES) {
    const value = boundedInset(inset?.[side]);
    root.style.setProperty(`${prefix}-${side}`, `${value}px`);
  }
}

function boundedInset(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0
    ? Math.min(number, MAX_SAFE_AREA_INSET)
    : 0;
}

function supportsVersion(webApp, requiredVersion) {
  if (typeof webApp?.isVersionAtLeast === "function") {
    try {
      return webApp.isVersionAtLeast(requiredVersion) === true;
    } catch {
      return false;
    }
  }
  return versionAtLeast(webApp?.version, requiredVersion);
}

function versionAtLeast(version, requiredVersion) {
  const actual = versionParts(version);
  const required = versionParts(requiredVersion);
  if (!actual || !required) {
    return false;
  }
  const length = Math.max(actual.length, required.length);
  for (let index = 0; index < length; index += 1) {
    const left = actual[index] ?? 0;
    const right = required[index] ?? 0;
    if (left !== right) {
      return left > right;
    }
  }
  return true;
}

function versionParts(value) {
  if (typeof value !== "string" || !/^\d+(?:\.\d+)*$/.test(value)) {
    return null;
  }
  return value.split(".").map(Number);
}
