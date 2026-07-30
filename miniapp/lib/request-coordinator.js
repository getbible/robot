export class LatestRequestCoordinator {
  #revision = 0;

  begin(context = {}) {
    return Object.freeze({
      revision: ++this.#revision,
      context,
    });
  }

  invalidate() {
    this.#revision += 1;
  }

  isCurrent(request) {
    return Boolean(request) && request.revision === this.#revision;
  }

  complete(request, operation) {
    if (!this.isCurrent(request)) {
      return false;
    }
    operation();
    return true;
  }
}
