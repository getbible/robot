/**
 * Canonical bookmark topics shared by the immutable global catalogue and UI
 * localization. Stable ids and English names remain storage/import migration
 * authority; localized names are display-only and are never synchronized.
 */
const LEGACY_NUMERIC_TOPIC_ID_PATTERN = /^[1-9]\d{0,2}$/;

export const CORE_BOOKMARK_TOPIC_DEFINITIONS = Object.freeze([
  topic("Adultery", "#f9a8b8"),
  topic("Authority of the Bible", "#93c5fd"),
  topic("Baptism", "#7dd3fc"),
  topic("Biblical Love", "#a16207"),
  topic("Blessings and Curses", "#fde68a", ["Blessings & Curses"]),
  topic("Christian Clothing", "#d8b4fe"),
  topic("Christian Offices", "#a5b4fc"),
  topic("Communion", "#fca5a5"),
  topic("Conditional Security", "#fdba74"),
  topic("Dating", "#f0abfc"),
  topic("Dietary Guidance", "#bef264"),
  topic("Discipline", "#fcd34d"),
  topic("Education", "#67e8f9"),
  topic("Effective Prayer", "#c4b5fd"),
  topic("Family Planning", "#fda4af"),
  topic("Fear Not", "#fef08a"),
  topic("First Day", "#fed7aa"),
  topic("Flattery", "#fecdd3"),
  topic("Free Will", "#99f6e4"),
  topic("God's Judgment", "#fb7185", ["God's Judgement"]),
  topic("Grace", "#bbf7d0"),
  topic("Home Church", "#86efac"),
  topic("Immutability", "#bfdbfe"),
  topic("Jesus Christ's Deity", "#c7d2fe"),
  topic("Jesus Christ's Humanity", "#fecaca"),
  topic("Leadership", "#a7f3d0"),
  topic("Longevity", "#d9f99d"),
  topic("Man's Role", "#bae6fd"),
  topic("Marriage", "#f5b7d2"),
  topic("Music's Influence", "#e9d5ff"),
  topic("No Fellowship", "#fdc4a8"),
  topic("No One is Good", "#fda4a4"),
  topic("Non-Resistance", "#a7f3e8", ["Nonresistance"]),
  topic("Not Under the Law", "#ddd6fe"),
  topic("Obey God's Commandments", "#fde047"),
  topic("Obey Government Laws", "#cbd5e1"),
  topic("Omnipotent", "#818cf8", ["Omnipotence"]),
  topic("Omnipresent", "#60a5fa", ["Omnipresence"]),
  topic("Omniscient", "#a78bfa", ["Omniscience"]),
  topic("Orderly Home", "#b7e4c7"),
  topic("Ordinances", "#67d6e8", ["Ordinance"]),
  topic("Prince of this World", "#c4a7a7"),
  topic("Providence", "#8dd3c7"),
  topic("Renewing of the Mind", "#a5f3fc"),
  topic("Repentance", "#fbbf8a"),
  topic("Saved by Faith", "#86efac"),
  topic("Sodomy", "#f59e9e"),
  topic("Spirit of Prophecy", "#d8b4fe"),
  topic("Spiritual Gifts", "#c084fc"),
  topic("Spiritual Judgment", "#f0c36e", ["Spiritual Judgement"]),
  topic("Spiritual Rebirth", "#6ee7b7"),
  topic("Temptation", "#fca5a5"),
  topic("What is Life", "#5eead4"),
  topic("Wine", "#e8a0bf"),
  topic("Wisdom Cause", "#fef08a"),
  topic("Wisdom Fruit", "#bef264"),
  topic("Wisdom Origin", "#fcd34d"),
  topic("Wisdom Value", "#fbbf24"),
  topic("Woman's Role", "#fbcfe8"),
  topic("Word of God", "#7dd3fc"),
  topic("Worldly Wisdom", "#d6d3d1"),
]);

export const ENGLISH_BOOKMARK_TOPIC_MESSAGES = Object.freeze(
  Object.fromEntries(CORE_BOOKMARK_TOPIC_DEFINITIONS.map((definition) => [
    definition.name_key,
    definition.name,
  ])),
);

export function isLegacyBookmarkTopicId(value) {
  return typeof value === "string" &&
    LEGACY_NUMERIC_TOPIC_ID_PATTERN.test(value);
}

function topic(name, color, aliases = [], defaultTopic = true) {
  const id = topicSlug(name);
  return Object.freeze({
    id,
    name,
    name_key: `bookmark_topics.${id}`,
    color,
    aliases: Object.freeze([...aliases]),
    default: defaultTopic,
  });
}

function topicSlug(name) {
  return String(name)
    .toLowerCase()
    .replace(/['’]/gu, "")
    .replace(/[^a-z0-9]+/gu, "-")
    .replace(/^-|-$/gu, "");
}
