from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path

import yaml

from mandate.scenario import ROLES

ROOT = Path(__file__).resolve().parents[2]
RULES_FILE = ROOT / "config" / "owners.yaml"
AGENT_IDS = {r for r, _ in ROLES}


@dataclass(frozen=True)
class Rule:
    match: dict[str, str]
    owner: str


@dataclass(frozen=True)
class Rules:
    default: str
    rules: tuple[Rule, ...]


def load_rules(path: Path = RULES_FILE) -> Rules:
    data = yaml.safe_load(path.read_text())
    default = str(data.get("default", "controller_a"))
    rules = []
    for r in data.get("rules") or []:
        owner = str(r["owner"])
        if owner not in AGENT_IDS:
            raise ValueError(f"owners.yaml: unknown agent id {owner!r}")
        rules.append(Rule(match={str(k): str(v) for k, v in (r.get("match") or {}).items()}, owner=owner))
    if default not in AGENT_IDS:
        raise ValueError(f"owners.yaml: unknown default agent id {default!r}")
    return Rules(default=default, rules=tuple(rules))


def assign(row: dict, rules: Rules) -> str:
    for rule in rules.rules:
        if all(fnmatch.fnmatchcase(str(row.get(col, "")).lower(), pat.lower()) for col, pat in rule.match.items()):
            return rule.owner
    return rules.default
