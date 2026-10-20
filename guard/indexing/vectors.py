"""Vector storage in MariaDB.

MariaDB 11.7+ stores vectors in a native `VECTOR` column with an HNSW index.
Other databases store float32 blobs and rank with numpy.
"""

from dataclasses import dataclass

import frappe
import numpy as np

from guard.utils import db

TABLE = "guard_vector"
DEFAULT_DIMENSIONS = 1024


@dataclass
class VectorRow:
	name: str
	kind: str
	ref: str
	repository: str | None
	vector: list[float]


class VectorStore:
	"""One table holds vectors of every kind: chunk, finding, context, kb."""

	@classmethod
	def dimensions(cls) -> int:
		return db.single_int("Guard Settings", "embedding_dimensions", DEFAULT_DIMENSIONS)

	@classmethod
	def native(cls) -> bool:
		if not hasattr(frappe.local, "guard_vector_native"):
			frappe.local.guard_vector_native = _supports_vector_type()
		return frappe.local.guard_vector_native

	@classmethod
	def ensure_table(cls) -> None:
		"""Create the table, or recreate it when the dimension changed."""
		dimensions = cls.dimensions()
		current = _column_type()
		if current and (not cls.native() or current == f"vector({dimensions})"):
			return
		if current:
			frappe.db.sql_ddl(f"DROP TABLE `{TABLE}`")
		column = f"VECTOR({dimensions}) NOT NULL" if cls.native() else "LONGBLOB NOT NULL"
		vector_index = ", VECTOR INDEX (`embedding`) M=8 DISTANCE=cosine" if cls.native() else ""
		frappe.db.sql_ddl(
			f"""CREATE TABLE `{TABLE}` (
				`name` VARCHAR(64) CHARACTER SET ascii NOT NULL PRIMARY KEY,
				`kind` VARCHAR(20) NOT NULL,
				`ref` VARCHAR(140) NOT NULL,
				`repository` VARCHAR(140),
				`embedding` {column},
				KEY `kind_repository` (`kind`, `repository`),
				KEY `ref` (`ref`){vector_index}
			) ENGINE=InnoDB"""
		)

	def upsert(self, rows: list[VectorRow]) -> None:
		dimensions = self.dimensions()
		data = [(r.name, r.kind, r.ref, r.repository, self._encode(r.vector, dimensions)) for r in rows]
		embedding_mark = "VEC_FromText(%s)" if self.native() else "%s"
		db.insert_many(
			TABLE,
			("name", "kind", "ref", "repository", "embedding"),
			data,
			placeholders=("%s", "%s", "%s", "%s", embedding_mark),
			replace=True,
			batch=100,
		)

	def delete_refs(self, kind: str, refs: list[str]) -> None:
		for start in range(0, len(refs), 500):
			batch = tuple(refs[start : start + 500])
			db.execute(f"DELETE FROM `{TABLE}` WHERE `kind` = %s AND `ref` IN %s", (kind, batch))

	def delete_repository(self, repository: str, kind: str | None = None) -> None:
		if kind:
			db.execute(f"DELETE FROM `{TABLE}` WHERE `repository` = %s AND `kind` = %s", (repository, kind))
		else:
			db.execute(f"DELETE FROM `{TABLE}` WHERE `repository` = %s", (repository,))

	def nearest(
		self,
		kind: str,
		vector: list[float],
		k: int = 10,
		repositories: list[str] | None = None,
	) -> list[tuple[str, float]]:
		"""Return (ref, cosine distance) pairs, closest first."""
		if self.native():
			return self._nearest_native(kind, vector, k, repositories)
		return self._nearest_numpy(kind, vector, k, repositories)

	def _nearest_native(self, kind, vector, k, repositories) -> list[tuple[str, float]]:
		conditions, values = _filters(kind, repositories)
		result = db.rows(
			f"""SELECT `ref`, VEC_DISTANCE_COSINE(`embedding`, VEC_FromText(%s)) AS distance
			FROM `{TABLE}` WHERE {conditions} ORDER BY distance LIMIT {int(k)}""",
			(self._encode(vector, self.dimensions()), *values),
		)
		return [(str(ref), float(distance)) for ref, distance in result]

	def _nearest_numpy(self, kind, vector, k, repositories) -> list[tuple[str, float]]:
		conditions, values = _filters(kind, repositories)
		result = db.rows(f"SELECT `ref`, `embedding` FROM `{TABLE}` WHERE {conditions}", values)
		if not result:
			return []
		matrix = np.vstack([np.frombuffer(bytes(blob), dtype=np.float32) for _, blob in result])
		query = _normalize(np.asarray(vector, dtype=np.float32))
		distances = 1 - _normalize_rows(matrix) @ query
		order = np.argsort(distances)[:k]
		return [(str(result[i][0]), float(distances[i])) for i in order]

	def _encode(self, vector: list[float], dimensions: int) -> str | bytes:
		array = np.zeros(dimensions, dtype=np.float32)
		size = min(dimensions, len(vector))
		array[:size] = np.asarray(vector[:size], dtype=np.float32)
		if self.native():
			return "[" + ",".join(f"{x:.6g}" for x in array.tolist()) + "]"
		return array.tobytes()


def _filters(kind: str, repositories: list[str] | None) -> tuple[str, tuple]:
	if repositories:
		return "`kind` = %s AND `repository` IN %s", (kind, tuple(repositories))
	return "`kind` = %s", (kind,)


def _normalize(vector: np.ndarray) -> np.ndarray:
	norm = np.linalg.norm(vector)
	return vector / norm if norm else vector


def _normalize_rows(matrix: np.ndarray) -> np.ndarray:
	norms = np.linalg.norm(matrix, axis=1, keepdims=True)
	norms[norms == 0] = 1
	return matrix / norms


def _column_type() -> str | None:
	if not db.rows("SHOW TABLES LIKE %s", (TABLE,)):
		return None
	result = db.rows(f"SHOW COLUMNS FROM `{TABLE}` LIKE 'embedding'")
	return str(result[0][1]).lower() if result else None


def _supports_vector_type() -> bool:
	if frappe.db.db_type != "mariadb":
		return False
	version = str(db.scalar("SELECT VERSION()"))
	major, minor = (int(part) for part in version.split("-")[0].split(".")[:2])
	return (major, minor) >= (11, 7)
