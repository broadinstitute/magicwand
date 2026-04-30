"""Built-in stdio parsers. Hardcoded; no plugin system.

A parser is just an entry in PARSERS: a list of (regex, cast) pairs. Each
regex has exactly one named group. When magicwand sees a bash command
starting with the parser's name, it tries each regex against every stdio
line; matches contribute `<name>.<group>: cast(value)` records to the
W&B run.

To add a new parser, edit this file. That's it.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, Iterator, List, Tuple


_Rule = Tuple[re.Pattern, Callable[[str], object]]


PARSERS: Dict[str, List[_Rule]] = {
    "bcftools": [
        (re.compile(r"SN\s+\d+\s+number of records:\s+(?P<records>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of no-ALTs:\s+(?P<no_alts>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of SNPs:\s+(?P<snps>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of MNPs:\s+(?P<mnps>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of indels:\s+(?P<indels>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of others:\s+(?P<others>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of multiallelic sites:\s+(?P<multiallelic>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of multiallelic SNP sites:\s+(?P<multiallelic_snps>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+number of singletons:\s+(?P<singletons>\d+)"), int),
        (re.compile(r"SN\s+\d+\s+ts/tv:\s+(?P<tstv>[0-9.]+)"), float),
        (re.compile(r"SN\s+\d+\s+ts/tv \(1st ALT\):\s+(?P<tstv_first_alt>[0-9.]+)"), float),
    ],
}


def parse_line(line: str, parser_name: str) -> Iterator[dict]:
    """Yield {parser.field: value} dicts for every regex that matches `line`."""
    for pat, cast in PARSERS.get(parser_name, []):
        m = pat.search(line)
        if not m:
            continue
        (k, v), = m.groupdict().items()
        yield {f"{parser_name}.{k}": cast(v)}


def known() -> List[str]:
    return sorted(PARSERS)
