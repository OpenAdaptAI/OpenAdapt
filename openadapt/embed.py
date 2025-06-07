"""This module handles the generation of text embeddings."""

from sentence_transformers import SentenceTransformer
from openadapt.custom_logger import logger
from openadapt.config import config # Import config

# Global variable to cache the loaded model
_model_cache = {}

def get_configured_model_name() -> str:
    """Returns the configured embedding model name."""
    return config.EMBEDDING_MODEL_NAME

def _load_model(model_name: str) -> SentenceTransformer | None:
    """Loads a sentence transformer model and caches it."""
    if model_name in _model_cache:
        return _model_cache[model_name]
    
    try:
        model = SentenceTransformer(model_name)
        logger.info(f"Loaded sentence transformer model: {model_name}")
        _model_cache[model_name] = model
        return model
    except Exception as e:
        logger.error(f"Failed to load sentence transformer model: {model_name}, {e}")
        _model_cache[model_name] = None # Cache None to avoid retrying failed loads repeatedly
        return None

def get_embedding(text: str, model_name: str | None = None) -> list[float] | None:
    """Generates an embedding for the given text using the specified or configured model.

    Args:
        text: The text to embed.
        model_name: Optional. The name of the sentence transformer model to use.
                    If None, uses the model from the global config.

    Returns:
        The embedding as a list of floats, or None if an error occurs.
    """
    current_model_name = model_name or get_configured_model_name()
    model = _load_model(current_model_name)

    if not model:
        logger.error(f"Sentence transformer model '{current_model_name}' not loaded. Cannot generate embedding.")
        return None
    if not text or not isinstance(text, str):
        logger.warning(f"Invalid text input for embedding: {text}")
        return None
    try:
        embedding_output = model.encode(text, convert_to_tensor=False, convert_to_numpy=False)
        # Ensure it's a list of Python floats
        if hasattr(embedding_output, 'tolist'): # Handles numpy array or torch tensor
            embedding = embedding_output.tolist()
        elif isinstance(embedding_output, list):
            embedding = embedding_output
        else:
            # Fallback if it's a single tensor/numpy scalar, though unlikely for sentence embeddings
            embedding = [float(embedding_output)]
        
        # Further ensure all elements are floats if it's a list of lists (some models might do this)
        if embedding and isinstance(embedding[0], list):
            embedding = [float(item) for sublist in embedding for item in sublist] # Flatten and convert
        elif embedding:
            embedding = [float(item) for item in embedding]
            
        return embedding
    except Exception as e:
        logger.error(f"Error generating embedding for text='{text[:100]}...': {e}")
        return None 