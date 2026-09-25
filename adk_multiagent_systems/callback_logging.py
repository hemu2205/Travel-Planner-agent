import logging
import sys

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse, LlmRequest


def _safe_str(text: str) -> str:
    """Safely sanitizes text for Windows console encoding."""
    if not text:
        return ""
    encoding = sys.stdout.encoding or "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(encoding)
    except Exception:
        return text.encode("ascii", errors="replace").decode("ascii")


def log_query_to_model(callback_context: CallbackContext, llm_request: LlmRequest):
    if llm_request.contents and llm_request.contents[-1].role == 'user':
        for part in llm_request.contents[-1].parts:
            if part.text:
                logging.info("[query to %s]: %s", callback_context.agent_name, _safe_str(part.text))

def log_model_response(callback_context: CallbackContext, llm_response: LlmResponse):
    if llm_response.content and llm_response.content.parts:
        for part in llm_response.content.parts:
            if part.text:
                logging.info("[response from %s]: %s", callback_context.agent_name, _safe_str(part.text))
            elif part.function_call:
                logging.info("[function call from %s]: %s", callback_context.agent_name, part.function_call.name)
