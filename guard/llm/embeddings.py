"""Text embeddings: the embedding model of its AI provider, or a hash embedding when there is none."""

import hashlib
import math
import re
from itertools import pairwise

from guard import settings as guard_settings
from guard.llm.openai_dialect import OpenAIDialect
from guard.llm.types import LLMError

BATCH = 64
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|\d+")
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


class Embedder:
	def __init__(self):
		config = guard_settings.gateway()
		self.route = config.route("embedding")
		self.dimensions = config.embedding_dimensions

	@property
	def uses_model(self) -> bool:
		return self.route.online

	def embed(self, texts: list[str]) -> list[list[float]]:
		if not texts:
			return []
		provider = self.route.ai_provider
		if not (provider and self.uses_model):
			return [hash_embedding(text, self.dimensions) for text in texts]
		# Grove serves embeddings on the OpenAI front for every dialect.
		dialect = OpenAIDialect(provider.url, provider.api_key, provider.timeout)
		vectors: list[list[float]] = []
		try:
			for start in range(0, len(texts), BATCH):
				batch = [text[:24000] or " " for text in texts[start : start + BATCH]]
				vectors.extend(dialect.embed(self.route.model, batch))
		except LLMError as error:
			# So that an outage opens the breaker of this AI provider, as for chat calls.
			error.ai_provider = error.ai_provider or provider.name
			raise
		from guard.llm import breaker

		breaker.record_success(provider.name)
		return vectors

	def embed_one(self, text: str) -> list[float]:
		return self.embed([text])[0]


def tokens(text: str) -> list[str]:
	"""Identifier-aware tokens: `getUserById` gives get, user, by, id."""
	out: list[str] = []
	for word in TOKEN.findall(text):
		parts = [p.lower() for piece in word.split("_") for p in CAMEL.findall(piece)] or [word.lower()]
		out.extend(parts)
	return out


def hash_embedding(text: str, dimensions: int) -> list[float]:
	"""Hash unigrams and bigrams into a signed, log-weighted, normalized vector."""
	vector = [0.0] * dimensions
	words = tokens(text)
	features = words + [f"{a} {b}" for a, b in pairwise(words)]
	counts: dict[str, int] = {}
	for feature in features:
		counts[feature] = counts.get(feature, 0) + 1
	for feature, count in counts.items():
		digest = hashlib.blake2b(feature.encode(), digest_size=8).digest()
		index = int.from_bytes(digest[:4], "little") % dimensions
		sign = 1.0 if digest[4] & 1 else -1.0
		vector[index] += sign * (1.0 + math.log(count))
	norm = math.sqrt(sum(x * x for x in vector)) or 1.0
	return [x / norm for x in vector]
