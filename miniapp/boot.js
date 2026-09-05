// Classic script, evaluated before the module graph is fetched.
//
// A module that fails to download, or that throws while evaluating, leaves
// the shell showing its opening spinner with nothing left to react: no code
// of the app ever runs. This watchdog turns that into the ordinary gate with
// a Try again button. It has no dependencies on purpose, so it works exactly
// when the rest of the client does not.
(function () {
  "use strict";

  var signal = window.__getbibleBoot || (window.__getbibleBoot = {});
  signal.started = false;
  signal.booting = false;
  signal.settled = false;
  // Every step of boot() has its own shorter bound; this is the last resort.
  var SETTLE_DEADLINE_MS = 120000;
  var shown = false;

  function element(id) {
    return document.getElementById(id);
  }

  function showGate(message) {
    if (shown || signal.settled) {
      return;
    }
    shown = true;
    var boot = element("boot-screen");
    var gate = element("access-denied");
    var text = element("access-message");
    var retry = element("access-retry");
    if (boot) {
      boot.hidden = true;
    }
    if (text) {
      text.textContent = message;
    }
    if (gate) {
      gate.hidden = false;
    }
    if (retry) {
      retry.addEventListener("click", function () {
        window.location.reload();
      });
      try {
        retry.focus({ preventScroll: true });
      } catch (error) {
        // Focus is a courtesy.
      }
    }
  }

  function checkStarted() {
    // Module scripts run before DOMContentLoaded. If boot() was not entered by
    // then, the graph failed to load or threw while evaluating.
    if (!signal.booting) {
      showGate(
        "getBible.Life could not start on this device. " +
          "Tap Try again to load it afresh.",
      );
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", checkStarted);
  } else {
    checkStarted();
  }
  window.setTimeout(function () {
    if (!signal.settled) {
      showGate(
        "getBible.Life is taking too long to open. " +
          "Tap Try again to reload it.",
      );
    }
  }, SETTLE_DEADLINE_MS);
})();
