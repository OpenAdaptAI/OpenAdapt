"""Vector similarity search functionality using pure Python."""

import json
import re
import time
from collections import Counter
from typing import List, Tuple

import numpy as np
from loguru import logger
from scipy.spatial.distance import cosine

from openadapt.db.crud import SaSession
from openadapt.embed import get_embedding, get_configured_model_name
from openadapt.models import Recording, RecordingEmbedding

# A small set of common English stop words to avoid external dependencies or downloads.
STOP_WORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "he",
    "in", "is", "it", "its", "of", "on", "that", "the", "to", "was", "were",
    "will", "with", "i", "you", "your", "d", "s", "t", "ve",
}


def _extract_context(recording: Recording) -> str:
    """Extract a general context from a recording to improve search relevance."""
    context_parts = []

    if recording.task_description:
        context_parts.append(recording.task_description)

    # Extract keywords from window titles
    if recording.window_events:
        all_titles = " ".join(
            [event.title for event in recording.window_events if event.title]
        )
        words = re.findall(r"\w+", all_titles.lower())
        meaningful_words = [
            word for word in words if word not in STOP_WORDS and not word.isdigit()
        ]

        if meaningful_words:
            # Add top 5 most common words as context
            word_counts = Counter(meaningful_words)
            for word, _ in word_counts.most_common(5):
                if len(word) > 2:  # filter out very short words
                    context_parts.append(word)

    # Extract context from action events
    if recording.action_events:
        # Get a summary of action types
        action_types = set(ae.name for ae in recording.action_events if ae.name)
        if action_types:
            context_parts.append("actions:" + ",".join(sorted(list(action_types))))

        # Detect significant text input
        typed_text = "".join(
            ae.key_char
            for ae in recording.action_events
            if ae.name == "keypress" and ae.key_char and ae.key_char.isalnum()
        )
        if len(typed_text) > 15:  # Heuristic for meaningful text input
            context_parts.append("text_input")

    # Add transcribed text from audio if available
    if hasattr(recording, "audio_info") and recording.audio_info:
        for audio in recording.audio_info:
            if audio.transcribed_text:
                context_parts.append("transcription:" + audio.transcribed_text)

    # Deduplicate and join
    unique_context = sorted(list(set(context_parts)))
    return " ".join(unique_context)


def get_enhanced_similarity_search(
    session: SaSession, query_text: str, top_n: int = 5
) -> List[Tuple[Recording, float]]:
    """Perform an enhanced search using application and action context.

    Args:
        session: The database session.
        query_text: The user's search query.
        top_n: The number of results to return.

    Returns:
        A list of (Recording, similarity_score) tuples.
    """
    model_name = get_configured_model_name()
    query_embedding = get_embedding(query_text, model_name=model_name)

    if not query_embedding:
        logger.error("Could not generate embedding for query text.")
        return []

    recordings_with_embeddings = (
        session.query(Recording, RecordingEmbedding)
        .join(RecordingEmbedding)
        .filter(RecordingEmbedding.model_name == model_name)
        .all()
    )

    results = []
    for recording, embedding_record in recordings_with_embeddings:
        enhanced_context = _extract_context(recording)
        context_embedding = get_embedding(enhanced_context, model_name=model_name)

        try:
            stored_embedding = (
                json.loads(embedding_record.embedding)
                if isinstance(embedding_record.embedding, str)
                else embedding_record.embedding
            )
            description_similarity = 1 - cosine(query_embedding, stored_embedding)
            context_similarity = (
                (1 - cosine(query_embedding, context_embedding))
                if context_embedding
                else 0.0
            )

            combined_similarity = (description_similarity * 0.6) + (
                context_similarity * 0.4
            )
            results.append((recording, combined_similarity))
        except (TypeError, json.JSONDecodeError) as e:
            logger.error(
                f"Error processing embedding for recording {recording.id}: {e}"
            )

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:top_n] 