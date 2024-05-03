from pprint import pformat

from loguru import logger
import cohere

from openadapt.config import config


def prompt(prompt: str, system_prompt: str | None = None) -> str:
    """Get prompt completion from Cohere.

    Args:
        prompt: the prompt to send to the chat model.
        system_prompt: an optional system prompt to provide context or specific instructions.

    Returns:
        A string containing the first message from the response.

    Raises:
        RuntimeError: If no response is returned or an error occurs in the API call.
    """
    co = cohere.Client(config.COHERE_API_KEY)
    
    # Prepare the chat history based on the provided system_prompt, if any
    chat_history = []
    if system_prompt:
        chat_history.append({"role": "SYSTEM", "message": system_prompt})
    
    # Call the Cohere chat API
    response = co.chat(
        chat_history=chat_history,
        message=prompt,
        connectors=[{"id": "web-search"}]  # Assuming the requirement to perform a web search
    )
    logger.info(f"response=\n{pformat(response)}")
    return response.text
