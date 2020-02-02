# import the Telegram API token from config.py
from config import (
    TRANSLATION,
    DEFAULT_VERSE,
    GETBIBLE_URL
)

# import the GetBible librarian
from getbible import GetBible

import re
import logging

# start getBible
_getBible = GetBible()


def _get_translation(input_string):
    """
    Extracts the translation from the input string.

    :param input_string: The string from which to extract the translation.
    :return: 'kjv' if the last word contains numbers, indicating it's not a translation, 
             otherwise returns the possible translation.
    """
    try:
        # Extract the last word which might be the translation or a part of the reference
        possible_translation = input_string.split()[-1]
    except IndexError:
        logging.info(f"No translation found, used default: {TRANSLATION}")
        return TRANSLATION

    # Check if the possible translation is valid
    if _getBible.valid_translation(possible_translation):
        return possible_translation
    else:
        logging.info(f"No valid translation found, used default: {TRANSLATION}")
        return TRANSLATION


def _get_references(input_string, translation):
    """
    Extracts and validates references from the input string, excluding the translation at the end if it's not 'kjv'.
    If the input string is empty, returns a default verse.

    :param input_string: The string containing references.
    :param translation: The translation to be excluded if present.
    :return: The validated references string, or raises ValueError if any reference is invalid.
    """
    # Check if input_string is empty or consists only of whitespace
    if not input_string.strip():
        logging.info(f"No reference found, used default: {DEFAULT_VERSE}")
        return DEFAULT_VERSE

    # Remove translation from the end if it's not 'kjv'
    references_string = re.sub(f' {translation}$', '', input_string) if translation != "kjv" else input_string

    # Split references_string by ';' and validate each reference
    references = references_string.split(';')
    for reference in references:
        if not _getBible.valid_reference(reference.strip(), translation):
            raise ValueError("Invalid reference found:", reference)

    return references_string


def _get_verse_reference(verses):
    """
    Generates a reference string for the verses.

    :param verses: A list of verse dictionaries.
    :return: A string representing the verse reference range.
    """
    verse_numbers = sorted([verse['verse'] for verse in verses])
    ranges = []
    range_start = None
    range_end = None

    for verse in verse_numbers:
        if range_start is None:
            range_start = verse
        elif verse == range_end + 1:
            range_end = verse
        else:
            if range_end:
                ranges.append(f"{range_start}-{range_end}")
            else:
                ranges.append(str(range_start))
            range_start = verse
            range_end = None
        if range_end is None:
            range_end = verse

    # Handling the case for the last verse or a single-verse range
    if range_start is not None:
        if range_start != range_end:
            ranges.append(f"{range_start}-{range_end}")
        else:
            ranges.append(str(range_start))

    return ','.join(ranges)


def _format_verses(result):
    """
    Formats the result from GetBible.select into nicely formatted paragraphs,
    including the translation name and verse reference range in the header.

    :param result: The dictionary result from GetBible.select
    :return: A string with the verses formatted as paragraphs.
    """
    formatted_paragraphs = []
    for key, value in result.items():
        book_name = value.get('book_name', '')
        chapter = value.get('chapter', '')
        abbreviation = value.get('abbreviation', '')
        verses = value.get('verses', [])

        # Generate verse reference range
        verse_ref = _get_verse_reference(verses)
        header = (f"[{book_name} {chapter}:{verse_ref}]({GETBIBLE_URL}{abbreviation}"
                  f"/{book_name}/{chapter}/{verse_ref}) ({abbreviation})")
        formatted_paragraphs.append(header)

        for verse in verses:
            verse_text = verse.get('text', '').strip()
            verse_number = verse.get('verse', '')
            verse_line = f"{verse_number}. {verse_text}"
            formatted_paragraphs.append(verse_line)

        formatted_paragraphs.append("")

    return "\n".join(formatted_paragraphs)


def _remove_prefix_case_insensitive(input_string, prefixes):
    # Join the prefixes into a regex pattern, making it case-insensitive
    # The ^ character ensures the match is at the start of the string
    regex_pattern = r'^(?:' + '|'.join(re.escape(prefix) for prefix in prefixes) + ')'
    # Use re.sub() to replace the matched prefix with '', case-insensitively
    cleaned_string = re.sub(regex_pattern, '', input_string, flags=re.IGNORECASE).strip()

    return cleaned_string


def _clean_string(input_string):
    # Define the prefixes you want to remove
    prefixes = ['/getBible', 'getBible', '/bible', 'Bible', '/get', 'Get']
    # Remove whitespace from both ends
    cleaned_string = _remove_prefix_case_insensitive(input_string, prefixes)

    return cleaned_string


def bible(text):
    try:
        text = _clean_string(text)
        translation = _get_translation(text)
        references = _get_references(text, translation)
        result = _getBible.select(references, translation)
        scripture = _format_verses(result)
    except Exception as e:
        logging.error(str(e))
        scripture = str(e)

    return scripture
