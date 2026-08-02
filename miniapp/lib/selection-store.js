import { selectionIdentity } from "./getbible-model.js";

const DEFAULT_MAXIMUM = 100;

export class BrowserSelectionError extends Error {
  constructor(message, code = "invalid_selection") {
    super(message);
    this.name = "BrowserSelectionError";
    this.code = code;
  }
}

export class BrowserSelectionStore {
  #registry = new Map();
  #items = [];
  #maximum;

  constructor({ maximum = DEFAULT_MAXIMUM } = {}) {
    this.#maximum = normalizeMaximum(maximum);
  }

  setMaximum(maximum) {
    this.#maximum = normalizeMaximum(maximum);
    if (this.#items.length > this.#maximum) {
      this.#items = this.#items.slice(0, this.#maximum);
    }
    return this.snapshot();
  }

  register(selection) {
    const item = normalizeSelection(selection);
    this.#registry.set(item.selection_id, item);
    return cloneSelection(item);
  }

  registerMany(selections) {
    if (!Array.isArray(selections)) {
      return [];
    }
    const registered = [];
    for (const selection of selections) {
      try {
        registered.push(this.register(selection));
      } catch {
        // One malformed upstream row must not disable valid rows.
      }
    }
    return registered;
  }

  hydrate(rawBasket) {
    const maximum = Number(rawBasket?.maximum);
    this.#maximum = normalizeMaximum(maximum);
    this.#items = [];
    for (const item of this.registerMany(rawBasket?.items)) {
      this.#appendUnique(item);
    }
    return this.snapshot();
  }

  add(selection) {
    const item = typeof selection === "string"
      ? this.#registry.get(selection)
      : this.register(selection);
    if (!item) {
      throw new BrowserSelectionError(
        "The selected Scripture is no longer available.",
      );
    }
    const identity = selectionIdentity(item);
    if (!identity) {
      throw new TypeError("Scripture selection identity is invalid.");
    }
    if (!this.#items.some((current) => selectionIdentity(current) === identity)) {
      if (this.#items.length >= this.#maximum) {
        throw new BrowserSelectionError("The Scripture basket is full.");
      }
      this.#items.push(cloneSelection(item));
    }
    return this.snapshot();
  }

  remove(selectionId) {
    const item = this.#registry.get(selectionId) ??
      this.#items.find((current) => current.selection_id === selectionId);
    const identity = item ? selectionIdentity(item) : "";
    this.#items = this.#items.filter((current) =>
      current.selection_id !== selectionId &&
      (!identity || selectionIdentity(current) !== identity)
    );
    return this.snapshot();
  }

  reorder(selectionIds) {
    if (!Array.isArray(selectionIds)) {
      throw new TypeError("selectionIds must be an array.");
    }
    const existing = new Map(
      this.#items.map((item) => [item.selection_id, item]),
    );
    if (
      selectionIds.length !== existing.size ||
      new Set(selectionIds).size !== selectionIds.length ||
      selectionIds.some((selectionId) => !existing.has(selectionId))
    ) {
      throw new BrowserSelectionError("Basket order is invalid.");
    }
    this.#items = selectionIds.map((selectionId) =>
      cloneSelection(existing.get(selectionId)),
    );
    return this.snapshot();
  }

  clear() {
    this.#items = [];
    return this.snapshot();
  }

  snapshot() {
    return {
      items: this.#items.map(cloneSelection),
      count: this.#items.length,
      maximum: this.#maximum,
    };
  }

  coordinates() {
    return this.#items.map((item) => ({
      translation: item.translation,
      book_number: item.book_number,
      chapter: item.chapter,
      verse: item.verse,
    }));
  }

  #appendUnique(item) {
    const identity = selectionIdentity(item);
    if (
      identity &&
      this.#items.length < this.#maximum &&
      !this.#items.some((current) => selectionIdentity(current) === identity)
    ) {
      this.#items.push(cloneSelection(item));
    }
  }
}

function normalizeMaximum(value) {
  return Number.isSafeInteger(value) && value > 0
    ? value
    : DEFAULT_MAXIMUM;
}

function normalizeSelection(selection) {
  if (!isSelectionDescriptor(selection)) {
    throw new TypeError("A valid Scripture selection is required.");
  }
  return {
    selection_id: selection.selection_id,
    translation: selection.translation,
    reference: selection.reference,
    book_number: selection.book_number,
    book_name: selection.book_name,
    chapter: selection.chapter,
    verse: selection.verse,
    text: selection.text,
    terms: Array.isArray(selection.terms) ? [...selection.terms] : [],
    highlights: Array.isArray(selection.highlights)
      ? selection.highlights.map((item) => ({ ...item }))
      : [],
  };
}

function isSelectionDescriptor(selection) {
  return Boolean(
    selection &&
    typeof selection === "object" &&
    !Array.isArray(selection) &&
    typeof selection.selection_id === "string" &&
    selection.selection_id.length > 0 &&
    typeof selection.translation === "string" &&
    selection.translation.length > 0 &&
    typeof selection.reference === "string" &&
    selection.reference.length > 0 &&
    Number.isSafeInteger(selection.book_number) &&
    selection.book_number > 0 &&
    typeof selection.book_name === "string" &&
    selection.book_name.length > 0 &&
    Number.isSafeInteger(selection.chapter) &&
    selection.chapter > 0 &&
    Number.isSafeInteger(selection.verse) &&
    selection.verse > 0 &&
    typeof selection.text === "string" &&
    selection.text.length > 0 &&
    selectionIdentity(selection).length > 0
  );
}

function cloneSelection(selection) {
  return {
    ...selection,
    terms: [...selection.terms],
    highlights: selection.highlights.map((item) => ({ ...item })),
  };
}
