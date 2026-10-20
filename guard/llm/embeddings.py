"""Text embeddings: the gateway model, or a hash embedding when there is none."""

import hashlib
import math
import re
from itertools import pairwise

from guard import settings as guard_settings
from guard.llm.openai_dialect import OpenAIDialect

BATCH = 64
TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9]*|\d+")
CAMEL = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


class Embedder:
	def __init__(self):
		self.config = guard_settings.gateway()
		self.dimensions = self.config.embedding_dimensions

	@property
	def uses_model(self) -> bool:
		return bool(self.config.url and self.config.embedding_model)

	def embed(self, texts: list[str]) -> list[list[float]]:
		if not texts:
			return []
		if not self.uses_model:
			return [hash_embedding(text, self.dimensions) for text in texts]
		# Grove serves embeddings on the OpenAI front for every dialect.
		dialect = OpenAIDialect(self.config.url, self.config.api_key, self.config.timeout)
		vectors: list[list[float]] = []
		for start in range(0, len(texts), BATCH):
			batch = [text[:24000] or " " for text in texts[start : start + BATCH]]
			vectors.extend(dialect.embed(self.config.embedding_model, batch))
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
