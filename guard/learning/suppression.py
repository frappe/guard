"""Drop findings the team has rejected before. Never drop P0 or security findings."""

from guard.indexing.vectors import VectorRow, VectorStore
from guard.llm.embeddings import Embedder
from guard.review.findings import Finding
from guard.utils import db

NEIGHBOR_DISTANCE = 0.15  # cosine 0.85
STRONG_DISTANCE = 0.07  # cosine 0.93
MIN_NEIGHBORS = 3
NOISE_WINDOW_DAYS = 90
NOISE_MIN_COUNT = 10
NOISE_MAX_RATE = 0.1


def text_of(finding: Finding) -> str:
	return f"{finding.title}\n{finding.body[:800]}"


def suppress(findings: list[Finding], repository: str | None, similar: bool = True) -> None:
	"""Drop findings the team ignores. `similar` False skips the comparison with past findings,
	which needs the embedding model."""
	if not repository:
		return
	candidates = [f for f in findings if f.kept and f.severity != "P0" and not f.is_security]
	if not candidates:
		return
	noisy = noisy_types(repository)
	for finding in candidates:
		if finding.severity == "P2" and finding.comment_type in noisy:
			finding.drop(
				"Suppressed",
				f"the team rarely addresses `{finding.comment_type}` comments in this repository",
			)
	if not similar:
		return
	store = VectorStore()
	vectors = Embedder().embed([text_of(f) for f in candidates])
	for finding, vector in zip(candidates, vectors, strict=True):
		if not finding.kept:
			continue
		neighbors = [
			(ref, d)
			for ref, d in store.nearest("finding", vector, k=12, repositories=[repository])
			if d <= NEIGHBOR_DISTANCE
		]
		if not neighbors:
			continue
		stats = _stats([ref for ref, _ in neighbors])
		score = sum(s["upvotes"] + s["addressed"] - 2 * s["downvotes"] - s["ignored"] for s in stats.values())
		if len(neighbors) >= MIN_NEIGHBORS and score < 0:
			finding.drop(
				"Suppressed", "similar to past findings the team rejected: " + ", ".join(stats)[:300]
			)
			continue
		ref, distance = neighbors[0]
		closest = stats.get(ref)
		if closest and distance <= STRONG_DISTANCE and closest["downvotes"] and not closest["upvotes"]:
			finding.drop("Suppressed", f"near-duplicate of down-voted finding {ref}")


def remember(names: dict[int, str], findings: list[Finding], repository: str | None) -> None:
	"""Store vectors of posted findings so later reviews can compare against them."""
	posted = [f for f in findings if f.kept and id(f) in names]
	if not posted or not repository:
		return
	vectors = Embedder().embed([text_of(f) for f in posted])
	VectorStore().upsert(
		[
			VectorRow(names[id(f)], "finding", names[id(f)], repository, v)
			for f, v in zip(posted, vectors, strict=True)
		]
	)


def noisy_types(repository: str) -> set[str]:
	rows = db.rows(
		f"""SELECT comment_type, COUNT(*), SUM(status = 'Addressed') FROM `tabReview Finding`
		WHERE repository = %s AND posted = 1 AND creation > NOW() - INTERVAL {NOISE_WINDOW_DAYS} DAY
		GROUP BY comment_type""",
		(repository,),
	)
	return {
		str(kind)
		for kind, total, addressed in rows
		if total >= NOISE_MIN_COUNT and (addressed or 0) / total < NOISE_MAX_RATE
	}


def _stats(names: list[str]) -> dict[str, dict[str, int]]:
	rows = db.dict_rows(
		"""SELECT name, IFNULL(upvotes, 0) AS upvotes, IFNULL(downvotes, 0) AS downvotes,
			status = 'Addressed' AS addressed, status = 'Ignored' AS ignored
		FROM `tabReview Finding` WHERE name IN %s""",
		(tuple(names),),
	)
	return {
		r["name"]: {k: int(r[k] or 0) for k in ("upvotes", "downvotes", "addressed", "ignored")} for r in rows
	}
