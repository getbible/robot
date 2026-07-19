from os import PathLike
from typing import Any

class GetBibleError(Exception): ...
class ReferenceValidationError(ValueError, GetBibleError): ...
class RequestLimitError(ReferenceValidationError): ...
class TranslationNotFoundError(FileNotFoundError, GetBibleError): ...
class RepositoryError(GetBibleError): ...
class CacheIntegrityError(GetBibleError): ...

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
        **kwargs: Any,
    ) -> None: ...
    def valid_translation(self, abbreviation: str) -> bool: ...
    def select(
        self,
        reference: str,
        abbreviation: str | None = ...,
    ) -> dict[str, Any]: ...
    def cache_info(self) -> dict[str, Any]: ...
    def close(self) -> None: ...
