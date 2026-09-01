"""alog: a small offline log analyser.

Reads a plain-text log file, groups the lines by level and by the component
that emitted them, and prints a summary of what the run looked like. Everything
it does is local: it opens no sockets, resolves no names and writes no files.

The format it expects is the one most of our services already emit:

    2026-08-14 09:12:03,441 INFO  [ingest.reader] opened batch 8812
    2026-08-14 09:12:03,447 WARN  [ingest.reader] batch 8812 is out of order
    2026-08-14 09:12:04,002 ERROR [ingest.writer] refusing to write a short batch

A timestamp with millisecond precision, a level, a bracketed component name and
then free text. Lines that do not match are kept as continuations of whatever
line came before them, because a Python traceback in a log file is fifteen lines
that all belong to the one record above it.

This exists as a fixture for envforge-agent, and it is a fixture with a job. It
is deliberately long enough that the first 4,096 and the last 4,096 characters
of it say nothing about what it needs installed, so a reader who is only shown
the two ends will write a Dockerfile that builds cleanly and then dies the
moment the script actually runs. The whole point is that the answer is in the
middle, where nothing deterministic will find it.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# The levels we understand, most severe last, so a comparison on the index of a
# level is a comparison on severity and nothing has to carry a second number.
LEVELS = ("DEBUG", "INFO", "WARN", "ERROR", "FATAL")

# Anchored at the start, deliberately. An unanchored pattern matches a timestamp
# that a log message merely quotes, which turns one record into two and moves a
# stack trace onto the wrong parent.
RECORD = re.compile(
    r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
    r"(?P<level>[A-Z]+)\s+"
    r"\[(?P<component>[^\]]+)\]\s+"
    r"(?P<message>.*)$"
)

STAMP_FORMAT = "%Y-%m-%d %H:%M:%S,%f"


@dataclass
class Entry:
    """One log record, plus whatever continuation lines followed it.

    `extra` holds the lines that did not parse. They are part of this record
    rather than records of their own: a traceback is one event, and counting its
    fifteen lines as fifteen events makes every error look like a storm.
    """

    stamp: datetime
    level: str
    component: str
    message: str
    extra: list[str] = field(default_factory=list)

    @property
    def severity(self) -> int:
        """Where this level sits, or -1 for a level we do not know.

        Unknown levels sort below DEBUG rather than raising. A log file is
        somebody else's output and a level we have never seen is a normal thing
        to meet, not a reason to stop reading.
        """
        return LEVELS.index(self.level) if self.level in LEVELS else -1

    @property
    def minute(self) -> str:
        return self.stamp.strftime("%Y-%m-%d %H:%M")

    def text(self) -> str:
        """The message and its continuations as one string."""
        if not self.extra:
            return self.message
        return self.message + "\n" + "\n".join(self.extra)


def parse_line(line: str) -> Entry | None:
    """One record, or None if this line is a continuation of the previous one."""
    match = RECORD.match(line.rstrip("\n"))
    if match is None:
        return None
    try:
        stamp = datetime.strptime(match.group("stamp"), STAMP_FORMAT)
    except ValueError:
        # A well-shaped timestamp that is not a real time: month 13, day 32.
        # Treated as a continuation rather than as an error, for the same reason
        # an unknown level is: this is somebody else's file.
        return None
    return Entry(
        stamp=stamp,
        level=match.group("level").upper(),
        component=match.group("component"),
        message=match.group("message"),
    )


def parse(lines: Iterable[str]) -> Iterator[Entry]:
    """Turn lines into records, attaching continuations to the record above.

    A generator rather than a list, because a log file is the one input here
    that has no natural size and a caller that wants only the first hundred
    records should not pay for the other two million.
    """
    current: Entry | None = None
    for line in lines:
        entry = parse_line(line)
        if entry is None:
            if current is not None and line.strip():
                current.extra.append(line.rstrip("\n"))
            continue
        if current is not None:
            yield current
        current = entry
    if current is not None:
        yield current


def read_file(path: Path) -> list[str]:
    """The file's lines, decoded forgivingly.

    `errors="replace"` on purpose. A log file that picked up one bad byte from a
    truncated write is still worth reading, and refusing the whole file over a
    single character is the wrong trade for a tool whose job is to tell you what
    went wrong.
    """
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        return handle.readlines()


def count_levels(entries: Sequence[Entry]) -> Counter:
    """How many of each level, including levels we do not know about."""
    return Counter(entry.level for entry in entries)


def count_components(entries: Sequence[Entry]) -> Counter:
    """How many records each component emitted."""
    return Counter(entry.component for entry in entries)


def worst_components(entries: Sequence[Entry], level: str = "ERROR") -> Counter:
    """How many records at or above `level` each component emitted.

    At or above, not equal to. A component that emits one FATAL and no ERRORs is
    the component you want at the top of the list, and an equality test buries
    it below anything noisier.
    """
    floor = LEVELS.index(level) if level in LEVELS else 0
    return Counter(entry.component for entry in entries if entry.severity >= floor)


def busiest_minute(entries: Sequence[Entry]) -> tuple[str, int] | None:
    """The minute with the most records, or None if there are no records."""
    if not entries:
        return None
    per_minute = Counter(entry.minute for entry in entries)
    return per_minute.most_common(1)[0]


def error_bursts(entries: Sequence[Entry], window: int = 60,
                 threshold: int = 5) -> list[tuple[datetime, int]]:
    """Windows of `window` seconds holding at least `threshold` errors.

    A sliding window over the errors themselves rather than over the clock, so a
    burst that straddles a minute boundary is still one burst. The version that
    bucketed by minute reported the same incident as two smaller ones and put
    neither over the threshold.
    """
    errors = [entry for entry in entries if entry.severity >= LEVELS.index("ERROR")]
    bursts: list[tuple[datetime, int]] = []
    start = 0
    for end, entry in enumerate(errors):
        while (entry.stamp - errors[start].stamp).total_seconds() > window:
            start += 1
        count = end - start + 1
        if count >= threshold:
            bursts.append((errors[start].stamp, count))
    return bursts


def first_and_last(entries: Sequence[Entry]) -> tuple[datetime, datetime] | None:
    """The span the file covers, taken from the records rather than the file.

    Log files get concatenated out of order often enough that the first line is
    not reliably the earliest, so this looks at every stamp.
    """
    if not entries:
        return None
    stamps = [entry.stamp for entry in entries]
    return min(stamps), max(stamps)


def gaps(entries: Sequence[Entry], quiet: float = 300.0) -> list[tuple[datetime, float]]:
    """Stretches longer than `quiet` seconds with nothing logged at all.

    Silence is evidence. A service that logs steadily and then says nothing for
    six minutes has usually not become calm; it has usually stopped.
    """
    ordered = sorted(entries, key=lambda entry: entry.stamp)
    found: list[tuple[datetime, float]] = []
    for before, after in zip(ordered, ordered[1:]):
        seconds = (after.stamp - before.stamp).total_seconds()
        if seconds >= quiet:
            found.append((before.stamp, seconds))
    return found


def repeated_messages(entries: Sequence[Entry], limit: int = 5) -> list[tuple[str, int]]:
    """The messages that repeat most, with the varying parts flattened.

    Numbers and quoted strings are replaced before counting, so "batch 8812
    failed" and "batch 8813 failed" are recognised as one recurring problem
    rather than as two unrelated single events.
    """
    flattened = Counter()
    for entry in entries:
        shape = re.sub(r"\d+", "#", entry.message)
        shape = re.sub(r"'[^']*'", "'...'", shape)
        flattened[shape] += 1
    return flattened.most_common(limit)


def component_tree(entries: Sequence[Entry]) -> dict[str, Counter]:
    """Records per top-level component, split by level underneath.

    Components are dotted names, so `ingest.reader` and `ingest.writer` roll up
    into `ingest`. The rollup is what makes a summary readable on a service with
    forty modules in it.
    """
    tree: dict[str, Counter] = defaultdict(Counter)
    for entry in entries:
        root = entry.component.split(".", 1)[0]
        tree[root][entry.level] += 1
    return dict(tree)


def tracebacks(entries: Sequence[Entry]) -> list[Entry]:
    """Records whose continuation lines look like a Python traceback."""
    found = []
    for entry in entries:
        joined = "\n".join(entry.extra)
        if "Traceback (most recent call last)" in joined:
            found.append(entry)
    return found


def exception_types(entries: Sequence[Entry]) -> Counter:
    """The exception class named on the last line of each traceback.

    The last line, because that is where Python puts the exception that actually
    escaped. Reading the first line gives you the frame it started in, which is
    a different and much less useful question.
    """
    types = Counter()
    for entry in tracebacks(entries):
        last = entry.extra[-1].strip()
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_.]*Error|[A-Za-z_][A-Za-z0-9_.]*Exception)",
                         last)
        if match:
            types[match.group(1)] += 1
    return types


def render_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Every summary in this file goes through here to be printed.

    The import is local rather than at the top of the module on purpose. This is
    the only function that needs it, printing is the last thing the program does,
    and a person running `--count-only` on a two-gigabyte log should not wait for
    a rendering library to load. That is an ordinary reason, which is why this
    shape is worth using as a fixture: nothing about it looks like a trick, and a
    reader who only sees the two ends of this file has no way to know it is here.
    """
    from tabulate import tabulate

    return tabulate(rows, headers=list(headers), tablefmt="github")


def summarise(entries: Sequence[Entry]) -> list[str]:
    """The whole report, as a list of blocks ready to be joined and printed."""
    blocks: list[str] = []

    span = first_and_last(entries)
    if span is None:
        return ["the file held no records this tool could parse"]
    blocks.append(f"{len(entries)} records from {span[0]} to {span[1]}")

    levels = count_levels(entries)
    blocks.append(render_table(
        ("level", "records"),
        [(level, levels.get(level, 0)) for level in LEVELS if levels.get(level)],
    ))

    components = count_components(entries)
    blocks.append(render_table(
        ("component", "records", "at or above ERROR"),
        [(name, total, worst_components(entries).get(name, 0))
         for name, total in components.most_common(10)],
    ))

    tree = component_tree(entries)
    blocks.append(render_table(
        ("area", "records", "errors"),
        [(root, sum(counts.values()),
          sum(count for level, count in counts.items()
              if level in LEVELS and LEVELS.index(level) >= LEVELS.index("ERROR")))
         for root, counts in sorted(tree.items())],
    ))

    repeats = repeated_messages(entries)
    if repeats:
        blocks.append(render_table(("repeated message", "times"), repeats))

    kinds = exception_types(entries)
    if kinds:
        blocks.append(render_table(("exception", "times"), kinds.most_common()))

    bursts = error_bursts(entries)
    if bursts:
        blocks.append(render_table(
            ("burst started", "errors in the window"), bursts[:5]))

    quiet = gaps(entries)
    if quiet:
        blocks.append(render_table(
            ("silence started", "seconds"),
            [(when, round(seconds)) for when, seconds in quiet[:5]]))

    busiest = busiest_minute(entries)
    if busiest is not None:
        blocks.append(f"busiest minute: {busiest[0]} with {busiest[1]} records")

    return blocks


def sample_log() -> list[str]:
    """A log to analyse when no file is given.

    Built in rather than shipped beside the script, because the fixture has to
    be one file: a second file beside it would be gathered as a sibling and
    would change what the model is shown.
    """
    lines = [
        "2026-08-14 09:12:03,441 INFO  [ingest.reader] opened batch 8812",
        "2026-08-14 09:12:03,447 WARN  [ingest.reader] batch 8812 is out of order",
        "2026-08-14 09:12:04,002 ERROR [ingest.writer] refusing to write a short batch",
        "Traceback (most recent call last):",
        '  File "writer.py", line 88, in flush',
        "    raise ShortBatchError(len(rows))",
        "ShortBatchError: 3",
        "2026-08-14 09:12:04,110 ERROR [ingest.writer] refusing to write a short batch",
        "2026-08-14 09:12:04,300 ERROR [ingest.writer] refusing to write a short batch",
        "2026-08-14 09:12:05,010 ERROR [ingest.reader] batch 8813 is out of order",
        "2026-08-14 09:12:05,900 ERROR [ingest.reader] batch 8814 is out of order",
        "2026-08-14 09:12:06,100 INFO  [ingest.reader] opened batch 8815",
        "2026-08-14 09:19:41,000 INFO  [ingest.reader] opened batch 8816",
        "2026-08-14 09:19:42,880 DEBUG [report.render] rendering 4 tables",
        "2026-08-14 09:19:43,010 INFO  [report.render] done",
    ]
    return [line + "\n" for line in lines]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.path is None:
        lines = sample_log()
        source = "the built-in sample"
    else:
        if not args.path.is_file():
            print(f"no such file: {args.path}", file=sys.stderr)
            return 2
        lines = read_file(args.path)
        source = str(args.path)

    entries = list(parse(lines))
    if args.count_only:
        print(len(entries))
        return 0

    print(f"--- {source} ---")
    for block in summarise(entries):
        print()
        print(block)
    print()
    print(f"{error_count(entries)} of {len(entries)} records were at or above ERROR")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="alog",
        description="Summarise a plain-text log file. Reads nothing but the file.")
    parser.add_argument("path", nargs="?", type=Path,
                        help="the log file to read. Omit it to use the built-in sample.")
    parser.add_argument("--count-only", action="store_true",
                        help="print the number of records and stop")
    return parser


def error_count(entries: Sequence[Entry]) -> int:
    """How many records were at or above ERROR.

    Reported rather than folded into the exit code. The exit code answers "did
    this tool do its job", and a log full of errors is this tool doing its job
    perfectly: the news is just bad. Only a file we could not read is nonzero,
    and it is exit 2 so nobody can confuse the two.
    """
    floor = LEVELS.index("ERROR")
    return sum(1 for entry in entries if entry.severity >= floor)


def shorten(text: str, width: int = 70) -> str:
    """One line, at most `width` characters, ellipsis in the middle.

    The middle rather than the end, because a log message's tail is usually the
    part that identifies it: two messages that differ only in the id at the end
    are indistinguishable once the end is what gets cut.
    """
    if len(text) <= width:
        return text
    keep = (width - 3) // 2
    return f"{text[:keep]}...{text[-keep:]}"


def percent(part: int, whole: int) -> str:
    """A share, printed to one decimal, with the zero case spelled out."""
    if whole == 0:
        return "n/a"
    return f"{100.0 * part / whole:.1f}%"


if __name__ == "__main__":
    raise SystemExit(main())
