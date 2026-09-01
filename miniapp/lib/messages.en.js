import { ENGLISH_BOOKMARK_TOPIC_MESSAGES } from "./bookmark-topic-definitions.js";

export const ENGLISH_MESSAGES = Object.freeze({
  ...ENGLISH_BOOKMARK_TOPIC_MESSAGES,
  "gate.opening": "Opening getBible.Life…",
  "gate.securing": "Securing your Telegram session…",
  "gate.protected": "Protected Mini App",
  "gate.title": "Open getBible.Life from Telegram",
  "gate.body":
    "This scripture experience is available only through the getBible.Life bot.",
  "gate.retry": "Try again",
  "gate.translations_unavailable":
    "No Bible translations are currently available.",
  "gate.verify_failed":
    "getBible.Life could not verify this Telegram session.",
  "gate.expired":
    "This launch is no longer active. Close getBible.Life, then send /bible or /search to the bot to start again.",
  "gate.close": "Close getBible.Life",
  "nav.label": "getBible.Life",
  "nav.home": "Home",
  "nav.search": "Search",
  "nav.bible": "Bible",
  "nav.selected": "Selected",
  "home.eyebrow": "The Holy Word of God",
  "home.title": "Read, find, and share His Word.",
  "home.body":
    "Gather all the Scripture you need, then post them together.",
  "home.tagline": "The words of eternal life",
  "home.search": "Search Scripture",
  "home.search_hint": "Find words, phrases, and themes",
  "home.browse": "Read the Bible",
  "home.browse_hint": "Read by chapter and select verses",
  "home.history": "See history",
  "home.history_hint": "Return to recently opened Scripture",
  "home.review": "Review",
  "home.manage": "Manage",
  "home.selection_one": "One verse selected",
  "home.selection_other": "{count} verses selected",
  "home.selection_hint": "Review, reorder, and post together",
  "home.history_preview": "Reading history",
  "home.history_preview_hint": "Review places you opened",
  "home.bookmarks_preview": "Bookmarks",
  "home.bookmarks_preview_hint": "Organize saved verses by topic",
  "search.eyebrow": "Find Scripture",
  "search.title": "Search the Word",
  "search.body":
    "Find a verse, select it where you read it, and build one clean post.",
  "search.label": "Search Scripture",
  "search.placeholder": "Search words or a phrase",
  "search.submit": "Search",
  "search.options": "Search options",
  "search.filters": "Filters",
  "search.sort_label": "Sort results",
  "search.sort_canonical": "Bible order",
  "search.sort_relevance": "Relevance",
  "search.clear": "Clear",
  "search.results_label": "Scripture search results",
  "search.load_more": "Load more",
  "search.enter_query": "Enter words or a phrase to search.",
  "search.found_one": "One verse found.",
  "search.found_other": "{count} verses found.",
  "search.more_loaded_one": "One more verse loaded.",
  "search.more_loaded_other": "{count} more verses loaded.",
  "search.failed": "Search did not complete",
  "search.no_results": "No verses found",
  "search.no_results_hint":
    "Try fewer words, a broader scope, or different filters.",
  "search.change_filters": "Change filters",
  "bible.eyebrow": "Read Scripture",
  "bible.title": "Choose a passage",
  "bible.body": "Open a chapter to read, then tap any verse you want to include.",
  "bible.navigation": "Bible navigation",
  "bible.translation": "Translation",
  "bible.book": "Book",
  "bible.chapter": "Chapter",
  "bible.choose_book": "Choose a book",
  "bible.choose_chapter": "Choose a chapter",
  "bible.chapter_number": "Chapter {number}",
  "bible.verses_label": "Chapter verses",
  "bible.load_failed": "Scripture did not load",
  "bible.no_verses": "No verses available",
  "bible.no_verses_hint": "Choose another chapter or translation.",
  "bible.choose_chapter_hint":
    "Select a chapter to read and choose its verses.",
  "bible.choose_book_hint":
    "Start with any available book in this translation.",
  "history.open": "Reading history",
  "history.eyebrow": "Your reading",
  "history.title": "History",
  "history.clear": "Clear all",
  "history.close": "Close reading history",
  "history.list_label": "Previously opened Scripture",
  "history.empty_title": "Your history will appear here",
  "history.empty_body":
    "Open a chapter or select a verse to return to it later.",
  "history.count_one": "One place",
  "history.count_other": "{count} places",
  "history.chapter": "Chapter opened",
  "history.selection": "Verse selected",
  "history.open_aria": "Open {reference} in {translation}",
  "history.remove_aria": "Remove {reference} from history",
  "history.clear_confirm": "Clear your entire reading history?",
  "history.cleared": "Reading history cleared.",
  "history.removed": "{reference} removed from history.",
  "history.verse_unavailable":
    "This verse is not available in the selected translation.",
  "bookmarks.eyebrow": "Your study",
  "bookmarks.title": "Bookmarks",
  "bookmarks.clear": "Clear personal",
  "bookmarks.count_one": "One bookmark",
  "bookmarks.count_other": "{count} bookmarks",
  "bookmarks.personal_verse_one": "One personal verse",
  "bookmarks.personal_verse_other": "{count} personal verses",
  "bookmarks.global_link_one": "One global link",
  "bookmarks.global_link_other": "{count} global links",
  "bookmarks.groups_label": "Bookmark topics",
  "bookmarks.search_label": "Find a bookmark topic",
  "bookmarks.search_placeholder": "Search topics",
  "bookmarks.no_match": "No topics match your search.",
  "bookmarks.manage_contribution": "Manage contribution",
  "bookmarks.manage_topics": "Manage topic names and colors",
  "bookmarks.topic_name": "Topic name",
  "bookmarks.topic_color": "Topic color",
  "bookmarks.new_topic": "New topic",
  "bookmarks.add_topic": "Add topic",
  "bookmarks.save_topic": "Save",
  "bookmarks.core_topic_name": "Built-in translated topic",
  "bookmarks.invalid_topic": "Enter a valid topic name and color.",
  "bookmarks.topic_limit":
    "You can create up to 100 topics. Remove one before adding another.",
  "bookmarks.restore_default_tags": "Restore default tags",
  "bookmarks.default_tags_restored_one":
    "One missing default tag was restored. Existing tags and bookmarks were kept.",
  "bookmarks.default_tags_restored_other":
    "{count} missing default tags were restored. Existing tags and bookmarks were kept.",
  "bookmarks.default_tags_current": "All default tags are already available.",
  "bookmarks.default_tags_limit":
    "Remove a custom topic before restoring the missing default tags.",
  "bookmarks.default_tags_failed": "The default tags could not be restored.",
  "bookmarks.all_topics": "All topics",
  "bookmarks.back_to_verse": "Back to verse",
  "bookmarks.group_empty_title": "No bookmarks in this topic yet",
  "bookmarks.group_empty_body":
    "Select a verse in the Bible, then choose this topic from the bookmark menu.",
  "bookmarks.list_label": "Saved bookmark verses",
  "bookmarks.global_title": "Global topics",
  "bookmarks.global_help":
    "Global topics are curated sets of verses that match each topic. Add or remove them one at a time or all at once, and create your own topics too.",
  "bookmarks.load_global_tags": "Load Global Tags",
  "bookmarks.add_all_global": "Add all",
  "bookmarks.remove_all_global": "Remove all",
  "bookmarks.global_info_aria": "About global topics",
  "bookmarks.global_clear_confirm":
    "Remove all global verse links? Your personal bookmarks and topics will remain. You can add the global topics again at any time.",
  "bookmarks.global_cleared":
    "All global verse links were removed. Personal bookmarks and topics were kept.",
  "bookmarks.global_loading": "Loading the global bookmark library…",
  "bookmarks.global_loaded":
    "Loaded {bookmarks} global verse links across {topics} topics.",
  "bookmarks.global_current":
    "{bookmarks} global verse links are active across {topics} topics.",
  "bookmarks.global_failed":
    "The global bookmark library could not be loaded. Please try again.",
  "bookmarks.global_topic_limit":
    "Remove a custom bookmark topic before loading the global topics.",
  "bookmarks.global_marker": "Global bookmark",
  "bookmarks.topic_global_title": "Global verses for this topic",
  "bookmarks.load_topic_global": "Load global verses",
  "bookmarks.reload_topic_global": "Reload global verses",
  "bookmarks.clear_topic_global": "Clear global verses",
  "bookmarks.topic_global_available":
    "{bookmarks} global verse links are available for this topic.",
  "bookmarks.topic_global_loaded":
    "{bookmarks} global verse links are loaded for this topic.",
  "bookmarks.topic_global_cleared":
    "Global verse links were cleared from this topic. Personal bookmarks were kept.",
  "bookmarks.backup_title": "Backup and restore",
  "bookmarks.backup_help":
    "Bookmarks sync across your Telegram devices. You can also keep a recovery copy in this private bot chat.",
  "bookmarks.backup": "Back up to chat",
  "bookmarks.download": "Download JSON",
  "bookmarks.import": "Import bookmarks",
  "bookmarks.storage_warning":
    "Telegram sync is unavailable on this client. Your local copy still works; back it up before clearing app data.",
  "bookmarks.assigned_topics": "Assigned bookmark topics",
  "bookmarks.choose_topic": "Add another topic",
  "bookmarks.more_topics": "Add to another topic…",
  "bookmarks.all_topics_assigned": "Added to every topic",
  "bookmarks.recent_topics": "Recently used",
  "bookmarks.clear_recent_topics": "Clear recent topics",
  "bookmarks.recent_topics_cleared": "Recently used topics cleared.",
  "bookmarks.none": "None",
  "bookmarks.close_palette": "Close bookmark menu",
  "bookmarks.palette_label": "Choose a bookmark topic for {reference}",
  "bookmarks.open_palette_aria": "Bookmark {reference}",
  "bookmarks.open_group_aria": "Open {name}, {count}",
  "bookmarks.open_topic_aria": "Open topic {name}",
  "bookmarks.open_global_topic_aria": "Open global topic {name}",
  "bookmarks.open_aria": "Open {reference} in {translation}",
  "bookmarks.open_global_aria":
    "Open {reference} in {translation}. Global bookmark.",
  "bookmarks.remove_aria": "Remove bookmark for {reference}",
  "bookmarks.remove_global_aria":
    "Hide global bookmark for {reference} from this browser",
  "bookmarks.remove_topic_assignment_aria":
    "Remove {reference} from {name}",
  "bookmarks.remove_global_topic_assignment_aria":
    "Hide global bookmark {reference} from {name} in this browser",
  "bookmarks.rename_aria": "Rename {name}",
  "bookmarks.color_aria": "Choose a color for {name}",
  "bookmarks.cancel_topic_edit": "Cancel editing",
  "bookmarks.save_topic_edit": "Save topic name",
  "bookmarks.delete_topic": "Delete topic",
  "bookmarks.remove_topic_aria": "Remove topic {name}",
  "bookmarks.clear_confirm":
    "Remove every personal bookmark? Global bookmarks and topics will remain.",
  "bookmarks.clear_done": "All personal bookmarks were removed.",
  "bookmarks.topic_delete_confirm_one":
    "Remove {name} and its saved verse link? Verses also saved in other topics stay there. This cannot be undone.",
  "bookmarks.topic_delete_confirm_other":
    "Remove {name} and its {count} saved verse links? Verses also saved in other topics stay there. This cannot be undone.",
  "bookmarks.topic_delete_global_confirm":
    "Remove {name}? Personal verse links removed: {personal}. Global verse links hidden until Global Tags are reloaded: {global}.",
  "bookmarks.topic_added": "Topic added.",
  "bookmarks.contribution_english_guidance":
    "Contributor topics must use an English name. Your topic and verse changes are sent securely for review after they are saved locally.",
  "bookmarks.contribution_english_required":
    "Use an English topic name before saving this contributor topic.",
  "bookmarks.contribution_sync_attention":
    "Your local bookmarks are safe. The contributor queue reached this device's safe limit, so the Mini App is reconciling a compact snapshot with the review server. Keep it open and online; new changes remain local and join the next recovery pass.",
  "bookmarks.contribution_storage_attention":
    "Your local bookmarks are safe, but this device could not preserve the contributor review queue. Keep the Mini App open and online while it retries, and do not clear its app data.",
  "bookmarks.contribution_disclosure":
    "You are enrolled as a GetBible contributor. Topic names must be in English. New topics and changes to topic verse assignments are saved on your device first and also sent securely to the project administrators for review. Approved changes may become part of the global topic library used by everyone.",
  "bookmarks.contribution_sync_title": "Contributor sync",
  "bookmarks.contribution_sync_now": "Sync now",
  "bookmarks.contribution_check_status": "Check status",
  "bookmarks.contribution_syncing": "Syncing…",
  "bookmarks.contribution_sync_progress":
    "Syncing… sending part {batch} of {total}. Your data stays safely on this device until the server confirms it.",
  "bookmarks.contribution_sync_idle":
    "Ready to sync your topics and verse links.",
  "bookmarks.contribution_sync_pending":
    "Your contributor application is waiting for approval. The bot will notify you, and this app checks periodically. Use Check status to check now.",
  "bookmarks.contribution_application_deferred":
    "Your contributor application needs further review. Use Check status to see whether an administrator has updated it.",
  "bookmarks.contribution_application_rejected":
    "Your contributor application was not accepted. Your personal topics and verse links remain safely on this device.",
  "bookmarks.contribution_access_revoked":
    "Contributor access is no longer active. Your personal topics and verse links remain safely on this device.",
  "bookmarks.contribution_sync_complete":
    "Everything is in sync. Topics still under review remain personal; published topics are marked G.",
  "bookmarks.contribution_sync_sent_one":
    "Sync complete. One update was sent for review.",
  "bookmarks.contribution_sync_sent_other":
    "Sync complete. {count} updates were sent for review.",
  "bookmarks.contribution_sync_waiting_one":
    "One local update is still waiting to be sent. We will retry automatically.",
  "bookmarks.contribution_sync_waiting_other":
    "{count} local updates are still waiting to be sent. We will retry automatically.",
  "bookmarks.contribution_sync_retry_wait":
    "The server asked us to wait before syncing again. We will retry automatically.",
  "bookmarks.contribution_sync_unavailable":
    "Contributor sync is temporarily unavailable. Your bookmarks remain safe on this device.",
  "bookmarks.contribution_sync_invalid_data":
    "The review server could not accept one saved contribution record. It remains personal and safe. After the app or server is updated, tap Sync again; if it continues, report any reference shown.",
  "bookmarks.contribution_sync_server_error":
    "The contributor review service is temporarily unavailable. Your bookmarks are safe; Sync will retry automatically.",
  "bookmarks.contribution_sync_update_error":
    "Contributor sync is unavailable on this server version. Your contribution remains personal and safe; Sync will work after the server update completes.",
  "bookmarks.contribution_sync_local_error":
    "The Mini App could not prepare some saved contribution data. It remains personal and safe. After the app is updated, tap Sync again.",
  "bookmarks.contribution_sync_network":
    "The connection was interrupted while sending. Nothing was lost; tap Sync to try again.",
  "bookmarks.contribution_sync_error":
    "Sync could not finish. Your contribution remains personal and safe; tap Sync again.",
  "bookmarks.contribution_sync_catalog_error":
    "Your updates reached the review server, but the global topic library could not be refreshed. Try Sync again.",
  "bookmarks.contribution_sync_reference": "Reference: {reference}",
  "bookmarks.contribution_pending_marker": "Personal contributor topic or verse link",
  "bookmarks.contribution_topic_outcomes": "Topics",
  "bookmarks.contribution_event_outcomes": "Updates",
  "bookmarks.contribution_outcome_published": "global {count}",
  "bookmarks.contribution_outcome_mapped":
    "accepted, awaiting publication {count}",
  "bookmarks.contribution_outcome_applied": "applied {count}",
  "bookmarks.contribution_outcome_approved": "accepted {count}",
  "bookmarks.contribution_outcome_pending": "awaiting review {count}",
  "bookmarks.contribution_outcome_deferred": "deferred {count}",
  "bookmarks.contribution_outcome_rejected": "rejected {count}",
  "bookmarks.topic_updated": "Topic updated.",
  "bookmarks.topic_removed": "Topic removed.",
  "bookmarks.last_topic": "Keep at least one bookmark topic.",
  "bookmarks.saved": "{reference} saved under {name}.",
  "bookmarks.limit_reached":
    "You can save up to 800 personal verses. Remove one before bookmarking another verse.",
  "bookmarks.verse_removed": "Bookmark removed from {reference}.",
  "bookmarks.removed": "{reference} removed from bookmarks.",
  "bookmarks.removed_from_topic": "{reference} removed from {name}.",
  "bookmarks.global_removed":
    "{reference} was hidden from this global topic. Reload the topic to restore it.",
  "bookmarks.backup_ready": "Bookmark backup downloaded.",
  "bookmarks.backup_sending": "Sending your bookmark backup to this private bot chat…",
  "bookmarks.backup_chat_ready": "Backup saved in this private bot chat.",
  "bookmarks.backup_failed": "The bot could not save that backup. Please try again.",
  "bookmarks.restore_confirm":
    "Merge {bookmarks} personal verses and {topics} topics from {file}? Existing verses keep their current topics and gain any missing topics from the backup.",
  "bookmarks.restore_loading": "Loading your bookmark backup from Telegram…",
  "bookmarks.restore_done": "Bookmark backup restored from Telegram.",
  "bookmarks.restore_failed":
    "That Telegram bookmark backup could not be restored. You can try its Restore button again.",
  "bookmarks.restore_ack_failed":
    "Bookmarks were restored, but Telegram could not mark the restore complete. Reopening it is safe.",
  "bookmarks.imported":
    "Imported changes for {bookmarks} verses and added {topics} topics. Unchanged existing verses: {conflicts}; skipped ranges: {ranges}; skipped notes: {notes}.",
  "bookmarks.import_limit":
    "That backup would exceed the 800-personal-verse or 100-topic limit. Remove some personal data, then try again.",
  "bookmarks.import_failed": "That bookmark backup could not be imported.",
  "bookmarks.global_verse_unavailable":
    "This global verse is not available in the selected translation.",
  "selection.eyebrow": "Your selection",
  "selection.title": "Ready to post",
  "selection.none": "No verses selected yet.",
  "selection.clear": "Clear",
  "selection.empty_title": "Your selected verses will appear here",
  "selection.empty_body":
    "Read or search, then tap a verse to add it.",
  "selection.browse": "Open the Bible",
  "selection.order_label": "Selected verses in posting order",
  "selection.post": "Post selected verses",
  "selection.posting": "Posting…",
  "selection.post_one": "Post one verse",
  "selection.post_other": "Post {count} verses",
  "selection.order_one": "One verse in posting order",
  "selection.order_other": "{count} verses in posting order",
  "selection.verse_added": "Verse added to your selection.",
  "selection.verse_removed": "Verse removed from your selection.",
  "selection.move_earlier": "Move {reference} earlier",
  "selection.move_later": "Move {reference} later",
  "selection.remove_aria": "Remove {reference}",
  "selection.clear_confirm":
    "Remove every verse from your current selection?",
  "selection.cleared": "Selection cleared.",
  "selection.posted": "Posted to Telegram.",
  "selection.post_failed": "Your verses were not posted",
  "filters.eyebrow": "Search settings",
  "filters.title": "Filter results",
  "filters.close": "Close filters",
  "filters.words": "Words",
  "filters.all": "All",
  "filters.any": "Any",
  "filters.phrase": "Phrase",
  "filters.match": "Match",
  "filters.whole_word": "Whole word",
  "filters.substring": "Substring",
  "filters.scope": "Scope",
  "filters.old": "Old",
  "filters.new": "New",
  "filters.other": "Other",
  "filters.books": "Books",
  "filters.select_all": "Select all",
  "filters.books_help": "Leave every book unchecked to search all books.",
  "filters.case": "Case sensitive",
  "filters.case_hint": "Match upper and lowercase exactly",
  "filters.diacritics": "Diacritics",
  "filters.fold": "Fold",
  "filters.exact": "Exact",
  "filters.exclude": "Exclude words",
  "filters.exclude_placeholder": "Optional, separated by spaces",
  "filters.proximity": "Maximum words apart",
  "filters.proximity_placeholder": "No limit",
  "filters.proximity_hint": "Available when matching all words.",
  "filters.reset": "Reset",
  "filters.apply": "Show results",
  "filters.loading_books": "Loading books…",
  "filters.no_books": "No books are available.",
  "connection.offline": "You’re offline. Reconnect to continue.",
  "translation.change_aria":
    "Change default translation, currently {translation}",
  "translation.save_failed":
    "Your choice works now, but could not be saved for next time.",
  "verse.add_aria": "Add {reference}: {text}",
  "verse.remove_aria": "Remove {reference}: {text}",
  "verse.count_one": "one verse",
  "verse.count_other": "{count} verses",
  "common.loading": "Loading…",
  "common.loading_scripture": "Loading Scripture.",
  "common.try_again": "Try again",
  "common.request_failed":
    "getBible.Life could not complete that request.",
  "error.session_invalid":
    "Your Telegram session is no longer valid. Reopen getBible.Life from the bot.",
  "error.selection_changed":
    "That selection changed. Refresh it and try again.",
  "error.rate_limited": "Please wait a moment before trying again.",
  "error.timeout": "The request took too long. Please try again.",
  "error.network":
    "getBible.Life could not connect. Check your connection and retry.",
  "error.invalid_response":
    "getBible.Life returned an unexpected response.",
  "error.not_found": "The requested information is no longer available.",
  "error.forbidden": "This request is not allowed.",
  "error.request_too_large": "That request is too large.",
  "error.scripture_unavailable":
    "Scripture is temporarily unavailable. Please try again.",
  "error.search_expired":
    "These search results are unavailable or expired. Search again.",
  "error.post_locked":
    "This selection already has an incomplete posting attempt. Review the target chat before creating a new selection.",
});
