"""Sanitized, actionable error classification. Full exceptions go to the local log only."""
import asyncio
import json
import httpx
from pydantic import ValidationError


class ProjectionError(ValueError):
    """The agent result could not be bound to verified evidence; nothing is shown."""


class ResultTooLarge(ProjectionError):
    pass


def classify(exc: BaseException) -> dict[str, str]:
    """Map an exception to {code, message, hint}. Never includes exception text, paths or credentials."""
    if isinstance(exc, ResultTooLarge):
        return dict(code='result_too_large', message='The audit finished but its result exceeded the safe size limit, so it is not shown.',
                    hint='Ask a narrower question (fewer topics or one filing) and try again.')
    if isinstance(exc, ProjectionError):
        return dict(code='result_rejected', message='The audit result failed an evidence-binding safety check, so no result is shown.',
                    hint='Try again. If it keeps happening, check the local server log.')
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return dict(code='model_timeout', message='The language-model step took too long and was stopped.',
                    hint='Try again in a moment; a shorter question can help.')
    if isinstance(exc, FileNotFoundError):
        return dict(code='model_unavailable', message='The Claude command-line tool could not be started on this machine.',
                    hint='Check that the Claude CLI is installed and on PATH, then try again.')
    if isinstance(exc, httpx.TimeoutException):
        return dict(code='retrieval_timeout', message='Searching the filings took too long and was stopped.',
                    hint='Check that the retrieval services are healthy, then try again.')
    if isinstance(exc, (httpx.HTTPError, ConnectionError, OSError)):
        return dict(code='retrieval_unavailable', message='The filing search service could not be reached.',
                    hint='Check that the local retrieval services are running, then try again.')
    if isinstance(exc, RuntimeError):
        text = str(exc)
        if text.startswith('Claude generation failed'):
            return dict(code='model_unavailable', message='The language model could not complete a step.',
                        hint='Check that the Claude CLI is signed in and reachable, then try again.')
        if text.startswith('hybrid HTTP'):
            return dict(code='retrieval_unavailable', message='The filing search service returned an error.',
                        hint='Check the local retrieval services, then try again.')
        if text.startswith('model output'):
            return dict(code='invalid_model_output', message='The language model returned an oversized response.', hint='Try again.')
    if isinstance(exc, (ValidationError, json.JSONDecodeError)):
        return dict(code='invalid_model_output', message='A model or service response did not match the required structure, so it was rejected.',
                    hint='Try again; a rephrased question can help.')
    if isinstance(exc, ValueError):
        return dict(code='evidence_rejected', message='A planning or evidence step failed its safety checks, so no result is shown.',
                    hint='Name the company, filing year and topics explicitly, then try again.')
    return dict(code='internal_error', message='The audit stopped because of an unexpected internal error.',
                hint='Try again. Details are in the local server log.')
