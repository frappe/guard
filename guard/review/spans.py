"""How much context to show for a file and a symbol."""

from dataclasses import dataclass, field


@dataclass
class ContextSpan:
	start: int  # 1-based, inclusive
	end: int  # 1-based, inclusive
	reason: str

	def covers(self, line: int) -> bool:
		return self.start <= line <= self.end

	def label(self, lines: int) -> str:
		first, last = max(self.start, 1), min(self.end, lines)
		if first == 1 and last >= lines:
			return "full file"
		return f"lines {first}-{last}"


@dataclass
class Hunk:
	start: int
	end: int
	label: str = "changed"


@dataclass
class ContextPlan:
	path: str
	spans: list[ContextSpan] = field(default_factory=list)

	def lines_to_show(self, hunks: list[Hunk], total: int) -> list[ContextSpan]:
		return self.spans or hunks_to_spans(hunks)


def hunks_to_spans(hunks: list[Hunk], pad: int = 5) -> list[ContextSpan]:
	return [ContextSpan(max(h.start - pad, 1), h.end + pad, h.label) for h in hunks]


def merge_spans(spans: list[ContextSpan], gap: int = 5) -> list[ContextSpan]:
	merged: list[ContextSpan] = []
	for span in sorted(spans, key=lambda s: s.start):
		if merged and span.start <= merged[-1].end + gap:
			merged[-1].end = max(merged[-1].end, span.end)
			if span.reason not in merged[-1].reason:
				merged[-1].reason += f", {span.reason}"
		else:
			merged.append(ContextSpan(span.start, span.end, span.reason))
	return merged


def symbol_span(start: int, end: int, pad: int = 3) -> ContextSpan:
	return ContextSpan(max(start - pad, 1), end + pad, "symbol")


def cap(spans: list[ContextSpan], budget: int) -> list[ContextSpan]:
	"""Truncate spans to a line budget, largest last."""
	out: list[ContextSpan] = []
	used = 0
	for span in sorted(spans, key=lambda s: s.end - s.start):
		size = span.end - span.start + 1
		if used + size > budget:
			break
		out.append(span)
		used += size
	return sorted(out, key=lambda s: s.start)
