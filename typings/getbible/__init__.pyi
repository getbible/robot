from os import PathLike
from typing import Any

class GetBibleError(Exception): ...
class ReferenceValidationError(ValueError, GetBibleError): ...
class RequestLimitError(ReferenceValidationError): ...
class TranslationNotFoundError(FileNotFoundError, GetBibleError): ...
class RepositoryError(GetBibleError): ...
class RepositoryResponseError(RepositoryError): ...
class RepositoryResponseTooLarge(RepositoryResponseError): ...
class CacheIntegrityError(GetBibleError): ...
class SearchValidationError(ValueError, GetBibleError): ...
class SearchLimitError(SearchValidationError, RequestLimitError): ...
class SearchDeadlineExceeded(SearchLimitError, TimeoutError): ...

class BookReference:
    book: int
    chapter: int
    verses: list[int]
    reference: str

class RequestLimits:
    def __init__(
        self,
        *,
        max_input_length: int = ...,
        max_references: int = ...,
        max_verses_per_reference: int = ...,
        max_total_verses: int = ...,
        max_search_offset: int = ...,
        max_search_books: int = ...,
        max_search_exclusions: int = ...,
    ) -> None: ...

class SearchLimits:
    def __init__(
        self,
        *,
        max_work_units: int = ...,
        max_response_bytes: int = ...,
        max_query_length: int = ...,
        max_query_terms: int = ...,
        min_substring_length: int = ...,
        max_books: int = ...,
        max_exclusions: int = ...,
        max_exclusion_terms: int = ...,
        max_offset: int = ...,
        max_limit: int = ...,
        deadline_seconds: float = ...,
        deadline_check_interval: int = ...,
    ) -> None: ...

class SearchBible:
    def __init__(
        self,
        *,
        words: str = ...,
        match: str = ...,
        case_sensitive: bool = ...,
        scope: str = ...,
        books: tuple[int | str, ...] = ...,
        diacritics: str = ...,
        exclude: tuple[str, ...] = ...,
        proximity: int | None = ...,
        sort: str = ...,
        limit: int = ...,
        offset: int = ...,
    ) -> None: ...

class GetBibleReference:
    def __init__(
        self,
        cache_limit: int | None = ...,
        *,
        max_reference_length: int = ...,
        max_verses: int = ...,
        max_verse_number: int = ...,
    ) -> None: ...
    def ref(
        self,
        reference: str,
        translation_code: str | None = ...,
    ) -> BookReference: ...
    def valid(
        self,
        reference: str,
        translation_code: str | None = ...,
    ) -> bool: ...

class GetBible:
    def __init__(
        self,
        repo_path: str | PathLike[str] = ...,
        version: str = ...,
        *,
        request_timeout: tuple[float, float] = ...,
        request_retries: int = ...,
        request_limits: RequestLimits | None = ...,
        negative_translation_cache_limit: int = ...,
        negative_translation_ttl: float = ...,
        max_response_bytes: int = ...,
        reference_cache_limit: int | None = ...,
        books_cache_limit: int | None = ...,
        chapter_cache_limit: int | None = ...,
        search_corpus_limit: int | None = ...,
        translation_cache_limit: int | None = ...,
        search_limits: SearchLimits | None = ...,
        **kwargs: Any,
    ) -> None: ...
    def valid_translation(self, abbreviation: str) -> bool: ...
    def select(
        self,
        reference: str,
        abbreviation: str | None = ...,
    ) -> dict[str, Any]: ...
    def search(
        self,
        query: str,
        abbreviation: str | None = ...,
        criteria: SearchBible | dict[str, Any] | str | None = ...,
    ) -> dict[str, Any]: ...
    def warm_translation(
        self,
        abbreviation: str | None = ...,
        *,
        case_sensitive: bool = ...,
        diacritics: str = ...,
    ) -> dict[str, Any]: ...
    def cache_info(self) -> dict[str, Any]: ...
    def close(self) -> None: ...
