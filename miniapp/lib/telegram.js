const LAUNCH_TOKEN_PATTERN = /^[A-Za-z0-9_-]{1,256}$/;

export class TelegramBridge {
  #webApp;
  #backHandler = null;
  #themeHandler;
  #viewportHandler;

  constructor(webApp = window.Telegram?.WebApp ?? null) {
    this.#webApp = webApp;
    this.#themeHandler = () => this.applyTheme();
    this.#viewportHandler = () => this.applyViewport();
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
    this.#webApp.onEvent?.("themeChanged", this.#themeHandler);
    this.#webApp.onEvent?.("viewportChanged", this.#viewportHandler);
    this.#webApp.expand?.();
    this.#webApp.enableVerticalSwipes?.();
    this.#webApp.setHeaderColor?.("bg_color");
    this.#webApp.setBackgroundColor?.("bg_color");
    this.#webApp.setBottomBarColor?.("secondary_bg_color");
    this.#webApp.ready?.();
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

  close() {
    this.#webApp?.close?.();
  }

  destroy() {
    this.setBackAction(null);
    this.#webApp?.offEvent?.("themeChanged", this.#themeHandler);
    this.#webApp?.offEvent?.("viewportChanged", this.#viewportHandler);
  }
}

export function validLaunchToken(value) {
  return typeof value === "string" && LAUNCH_TOKEN_PATTERN.test(value)
    ? value
    : null;
}
