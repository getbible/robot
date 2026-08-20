import west from "./bookmark-locales-west.js";
import europe from "./bookmark-locales-europe.js";
import asia from "./bookmark-locales-asia.js";
import community from "./bookmark-locales-community.js";
import minority from "./bookmark-locales-minority.js";
import heritage from "./bookmark-locales-heritage.js";
import { ENGLISH_MESSAGES } from "./messages.en.js";

const localeGroups = Object.freeze([
  west,
  europe,
  asia,
  community,
  minority,
  heritage,
]);

const rawCatalogs = {};
for (const group of localeGroups) {
  for (const [locale, catalog] of Object.entries(group)) {
    if (Object.hasOwn(rawCatalogs, locale)) {
      throw new TypeError(`Duplicate bookmark locale: ${locale}.`);
    }
    rawCatalogs[locale] = catalog;
  }
}

const extensionKeys = Object.freeze(Object.keys(rawCatalogs.af));
const englishExtension = Object.freeze(
  Object.fromEntries(
    extensionKeys.map((key) => {
      if (typeof ENGLISH_MESSAGES[key] !== "string") {
        throw new TypeError(`Missing English bookmark message: ${key}.`);
      }
      return [key, ENGLISH_MESSAGES[key]];
    }),
  ),
);

/**
 * Existing whole-application language-source policies. The extension follows
 * these sources too, so adding Bookmarks and History cannot turn one screen
 * into a mixture of the established UI language and a newly inferred one.
 * `id` is not a separately exposed UI locale; `ppk` already carries the
 * established Indonesian policy catalog.
 */
export const BOOKMARK_LOCALE_POLICY_SOURCES = Object.freeze({
  chr: "en",
  cop: "ar",
  cu: "ru",
  enm: "en",
  got: "de",
  grc: "el",
  he: "hbo",
  mlf: "en",
  pon: "en",
  pot: "en",
  ppk: "id",
  rmq: "es",
  syr: "ar",
  tlh: "en",
  tsg: "tl",
  "zh-hans": "zh",
});

// Integer counts in Czech, Polish, Russian, and Ukrainian need a distinct
// 2–4 (`few`) form. Russian, Ukrainian, and Tagalog also apply CLDR `one` to
// integers other than 1, so those templates carry the numeric placeholder.
const pluralExtensions = Object.freeze({
  cs: Object.freeze({
    "bookmarks.count_few": "{count} záložky",
    "bookmarks.personal_verse_few": "{count} osobní verše",
    "bookmarks.global_link_few": "{count} globální odkazy",
    "bookmarks.default_tags_restored_few":
      "Byly obnoveny {count} chybějící výchozí štítky. Stávající štítky a záložky zůstaly zachovány.",
    "bookmarks.topic_delete_confirm_few":
      "Odstranit {name} a jeho {count} uložené odkazy na verše? Verše uložené také v jiných tématech tam zůstanou. Tuto akci nelze vrátit zpět.",
  }),
  pl: Object.freeze({
    "bookmarks.count_few": "{count} zakładki",
    "bookmarks.personal_verse_few": "{count} osobiste wersety",
    "bookmarks.global_link_few": "{count} globalne łącza",
    "bookmarks.default_tags_restored_few":
      "Przywrócono {count} brakujące etykiety domyślne. Istniejące etykiety i zakładki zachowano.",
    "bookmarks.topic_delete_confirm_few":
      "Usunąć {name} i zapisane w nim łącza do wersetów ({count})? Wersety zapisane także w innych tematach pozostaną w nich. Tej czynności nie można cofnąć.",
  }),
  ru: Object.freeze({
    "bookmarks.count_one": "{count} закладка",
    "bookmarks.personal_verse_one": "{count} личный стих",
    "bookmarks.global_link_one": "{count} глобальная ссылка",
    "bookmarks.default_tags_restored_one":
      "Восстановлена {count} отсутствующая метка по умолчанию. Существующие метки и закладки сохранены.",
    "bookmarks.topic_delete_confirm_one":
      "Удалить тему {name} и сохранённую в ней {count} ссылку на стих? Стихи, сохранённые также в других темах, останутся там. Это действие нельзя отменить.",
    "bookmarks.count_few": "{count} закладки",
    "bookmarks.personal_verse_few": "{count} личных стиха",
    "bookmarks.global_link_few": "{count} глобальные ссылки",
    "bookmarks.default_tags_restored_few":
      "Восстановлены {count} отсутствующие метки по умолчанию. Существующие метки и закладки сохранены.",
    "bookmarks.topic_delete_confirm_few":
      "Удалить тему {name} и сохранённые в ней ссылки на стихи ({count})? Стихи, сохранённые также в других темах, останутся там. Это действие нельзя отменить.",
  }),
  uk: Object.freeze({
    "bookmarks.count_one": "{count} закладка",
    "bookmarks.personal_verse_one": "{count} особистий вірш",
    "bookmarks.global_link_one": "{count} глобальне посилання",
    "bookmarks.default_tags_restored_one":
      "Відновлено {count} відсутній тег за замовчуванням. Існуючі теги та закладки збережено.",
    "bookmarks.topic_delete_confirm_one":
      "Видалити тему {name} і збережене в ній {count} посилання на вірш? Вірші, також збережені в інших темах, залишаються там. Це неможливо скасувати.",
    "bookmarks.count_few": "{count} закладки",
    "bookmarks.personal_verse_few": "{count} особисті вірші",
    "bookmarks.global_link_few": "{count} глобальні посилання",
    "bookmarks.default_tags_restored_few":
      "Відновлено {count} відсутні теги за замовчуванням. Існуючі теги та закладки збережено.",
    "bookmarks.topic_delete_confirm_few":
      "Видалити {name} і його {count} збережені посилання на вірші? Вірші, також збережені в інших темах, залишаються там. Це неможливо скасувати.",
  }),
  tl: Object.freeze({
    "bookmarks.count_one": "{count} bookmark",
    "bookmarks.personal_verse_one": "{count} personal na taludtod",
    "bookmarks.global_link_one": "{count} pandaigdigang link",
    "bookmarks.default_tags_restored_one":
      "Na-restore ang {count} nawawalang default na tag. Ang mga kasalukuyang tag at bookmark ay itinago.",
    "bookmarks.topic_delete_confirm_one":
      "Alisin ang {name} at ang {count} naka-save na verse link nito? Ang mga talatang naka-save din sa ibang mga paksa ay nananatili doon. Hindi na ito maaaring bawiin.",
  }),
});

const sourceCatalogs = Object.fromEntries(
  Object.entries(rawCatalogs).map(([locale, catalog]) => [
    locale,
    pluralExtensions[locale]
      ? Object.freeze({ ...catalog, ...pluralExtensions[locale] })
      : catalog,
  ]),
);

const catalogs = {};
for (const locale of Object.keys(rawCatalogs)) {
  const source = BOOKMARK_LOCALE_POLICY_SOURCES[locale];
  if (source === "en") {
    catalogs[locale] = englishExtension;
    continue;
  }
  if (source === "id") {
    catalogs[locale] = sourceCatalogs[locale];
    continue;
  }
  if (source) {
    if (!sourceCatalogs[source]) {
      throw new TypeError(`Missing bookmark locale policy source: ${source}.`);
    }
    catalogs[locale] = sourceCatalogs[source];
    continue;
  }
  catalogs[locale] = sourceCatalogs[locale];
}

/**
 * Complete additions for the Home summary, History excerpt state, Bookmarks,
 * and stable built-in bookmark-topic names in every existing UI catalog.
 */
export const BOOKMARK_LOCALE_EXTENSION = Object.freeze(catalogs);

/**
 * Backward-compatible name for callers that consumed the initial fallback
 * metadata. It now exposes every governed alias/fallback source, not only the
 * low-resource subset.
 */
export const BOOKMARK_LOCALE_FALLBACK_POLICIES =
  BOOKMARK_LOCALE_POLICY_SOURCES;

export default BOOKMARK_LOCALE_EXTENSION;
