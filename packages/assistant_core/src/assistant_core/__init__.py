"""Independent documentation retrieval and grounded DeepSeek generation."""
from .rag import Document, Chunk, FastEmbedder, Index, load_documents, documentation_paths, chunk_document
from .provider import DeepSeek, grounded_answer, validate_answer, ProviderError
